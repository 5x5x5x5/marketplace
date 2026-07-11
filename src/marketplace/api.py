"""FastAPI app.

Three routers:
  /...             buyer-facing
  /...             seller-facing
  /admin/...       operator-facing (the only routes that see both sides' numbers)

Identity comes from the authenticated principal (`auth.py`), never from a
request body or query param. Every buyer route derives `buyer_id` from a buyer
token; every seller route derives `seller_id` from a seller token; `/admin/*`
requires an admin token. Buyer and seller responses use distinct view models
that omit the other side's number — enforced by the model layer, see
`BuyerJobView`/`SellerJobView` in `models.py`.

Single global Config + Store instances. Module-level so tests can patch by
import. Reset between tests via `reset_state()`. A single module lock serializes
state transitions (quote consume, job status, availability).
"""

import math
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .auth import current_buyer, current_seller, require_admin
from .config import Config, MarginFloor, Pipelines
from .matching import STRATEGIES, SellerCandidate
from .models import (
    AvailabilityRequest,
    BuyerJobView,
    Job,
    JobCreateRequest,
    JobStatus,
    MarginFloorBody,
    MatchingStrategyBody,
    PipelinesBody,
    Quote,
    QuoteRequest,
    SellerJobView,
    ServiceType,
    ServiceTypeBody,
    Side,
    Transaction,
)
from .pricing import REGISTRY, PricingContext, run_pipeline
from .store import Store

QUOTE_TTL = timedelta(minutes=5)
MAX_ACTIVE_QUOTES = 10_000  # ponytail: crude OOM backstop; real limits at the gateway/DB.
MAX_PAGE = 200

# ponytail: one global lock serializes state transitions. Correct and simple at
# pilot scale; swap for per-job / per-account locks or DB row locks if
# throughput ever demands it.
_LOCK = threading.Lock()


# ---------- Module-level singletons ----------

config = Config()
store = Store()


def reset_state() -> None:
    """Reset the global Config and Store. Used by tests."""
    global config, store
    config = Config()
    store = Store()


# ---------- Quote orchestration ----------


def _compute_buyer_price(
    service_type: ServiceType, buyer_id: str, supply: int, demand: int
) -> float:
    pipelines = config.get_pipelines(service_type.id)
    ctx = PricingContext(
        side=Side.BUYER,
        service_type=service_type,
        buyer_id=buyer_id,
        buyer_profile=store.get_or_create_buyer(buyer_id),
        live_supply=supply,
        live_demand=demand,
    )
    return run_pipeline(service_type.base_buyer_price, pipelines.buyer, ctx, config.adjuster_params)


def _probe_min_payout(
    service_type: ServiceType, supply: int, demand: int
) -> tuple[float | None, str | None]:
    """Return (min_payout, seller_id) over currently-available sellers,
    or (None, None) if no sellers are available."""
    avails = store.available_for(service_type.id)
    if not avails:
        return None, None
    pipelines = config.get_pipelines(service_type.id)
    best: tuple[float, str] | None = None
    for a in avails:
        profile = store.get_or_create_seller(a.seller_id)
        ctx = PricingContext(
            side=Side.SELLER,
            service_type=service_type,
            seller_id=a.seller_id,
            seller_profile=profile,
            live_supply=supply,
            live_demand=demand,
        )
        payout = run_pipeline(
            service_type.base_seller_payout, pipelines.seller, ctx, config.adjuster_params
        )
        if best is None or payout < best[0]:
            best = (payout, a.seller_id)
    if best is None:
        return None, None
    return best[0], best[1]


def _floor_for(buyer_price: float) -> float:
    return max(config.margin_floor.absolute, config.margin_floor.pct * buyer_price)


# ---------- Buyer router ----------

buyer_router = APIRouter(tags=["buyer"])


@buyer_router.post("/quotes", response_model=Quote)
def create_quote(req: QuoteRequest, buyer_id: str = Depends(current_buyer)) -> Quote:
    with _LOCK:
        service_type = config.service_types.get(req.service_type_id)
        if service_type is None:
            raise HTTPException(status_code=404, detail="unknown service_type_id")

        store.sweep_expired_quotes(datetime.now(UTC))
        if len(store.quotes) >= MAX_ACTIVE_QUOTES:
            raise HTTPException(status_code=503, detail="quote capacity reached, retry shortly")

        supply = len(store.available_for(service_type.id))
        # Demand proxy: existing active jobs for this service + this fresh request.
        demand = store.active_demand(service_type.id) + 1

        buyer_price = round(_compute_buyer_price(service_type, buyer_id, supply, demand), 2)
        probe_payout, _ = _probe_min_payout(service_type, supply, demand)

        if probe_payout is not None:
            probe_payout = round(probe_payout, 2)
            floor = _floor_for(buyer_price)
            if buyer_price - probe_payout < floor:
                # Round the corrected price UP to a whole unit so buyer_price is
                # not pinned to exactly probe_payout + floor (which would let the
                # buyer back out the seller's payout).
                target = float(math.ceil(probe_payout + floor))
                ceiling = service_type.base_buyer_price * config.margin_floor.ceiling_multiplier
                if target > ceiling:
                    raise HTTPException(
                        status_code=422,
                        detail="cannot quote: required margin exceeds the configured price ceiling",
                    )
                buyer_price = target

        quote = Quote(
            buyer_id=buyer_id,
            service_type_id=service_type.id,
            buyer_price=round(buyer_price, 2),
            expires_at=datetime.now(UTC) + QUOTE_TTL,
        )
        store.quotes[quote.id] = quote
    return quote


@buyer_router.post("/jobs", response_model=BuyerJobView)
def create_job(req: JobCreateRequest, buyer_id: str = Depends(current_buyer)) -> BuyerJobView:
    with _LOCK:
        quote = store.quotes.get(req.quote_id)
        # Same 404 whether the quote is missing or belongs to another buyer —
        # don't confirm existence of someone else's quote.
        if quote is None or quote.buyer_id != buyer_id:
            raise HTTPException(status_code=404, detail="quote not found")
        if quote.expires_at < datetime.now(UTC):
            raise HTTPException(status_code=410, detail="quote expired")

        service_type = config.service_types.get(quote.service_type_id)
        if service_type is None:
            raise HTTPException(status_code=409, detail="service_type no longer configured")

        job = Job(
            quote_id=quote.id,
            buyer_id=quote.buyer_id,
            service_type_id=quote.service_type_id,
            buyer_price=quote.buyer_price,
            status=JobStatus.QUOTED,
        )

        # Run the matching strategy now to decide which seller is offered the job.
        avails = store.available_for(service_type.id)
        candidates = [
            SellerCandidate(
                seller_id=a.seller_id,
                profile=store.get_or_create_seller(a.seller_id),
                available_since=a.since,
            )
            for a in avails
        ]
        strategy = STRATEGIES.get(config.matching_strategy)
        if strategy is None:
            raise HTTPException(
                status_code=500, detail=f"unknown strategy {config.matching_strategy!r}"
            )

        supply = len(candidates)
        demand = store.active_demand(service_type.id) + 1
        result = strategy(job, candidates, config, {"supply": supply, "demand": demand})
        if result is None:
            raise HTTPException(
                status_code=422, detail="no available seller meets the margin floor"
            )

        job.seller_id = result.seller_id
        job.seller_payout = round(result.seller_payout, 2)
        store.jobs[job.id] = job
        # Consume the quote — quotes are single-use.
        store.quotes.pop(quote.id, None)
    return BuyerJobView.from_job(job)


@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(job_id: UUID, buyer_id: str = Depends(current_buyer)) -> BuyerJobView:
    job = store.jobs.get(job_id)
    # Only the owning buyer sees the buyer view. Same 404 for missing vs
    # not-yours so a caller can't probe others' jobs.
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    return BuyerJobView.from_job(job)


# ---------- Seller router ----------

seller_router = APIRouter(tags=["seller"])


@seller_router.post("/availability")
def post_availability(
    req: AvailabilityRequest, seller_id: str = Depends(current_seller)
) -> dict[str, str]:
    if req.service_type_id not in config.service_types:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    with _LOCK:
        store.add_availability(seller_id, req.service_type_id)
    return {"status": "ok"}


@seller_router.delete("/availability/{service_type_id}")
def delete_availability(
    service_type_id: str, seller_id: str = Depends(current_seller)
) -> dict[str, str]:
    with _LOCK:
        removed = store.remove_availability(seller_id, service_type_id)
    if not removed:
        raise HTTPException(status_code=404, detail="availability not found")
    return {"status": "ok"}


@seller_router.get("/jobs/offered", response_model=list[SellerJobView])
def list_offered(
    seller_id: str = Depends(current_seller),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> list[SellerJobView]:
    offered = [
        j for j in store.jobs.values() if j.seller_id == seller_id and j.status == JobStatus.QUOTED
    ]
    return [SellerJobView.from_job(j) for j in offered[offset : offset + limit]]


@seller_router.post("/jobs/{job_id}/accept")
def accept_job(job_id: UUID, seller_id: str = Depends(current_seller)) -> dict[str, str]:
    with _LOCK:
        job = store.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.seller_id != seller_id:
            raise HTTPException(status_code=403, detail="job not offered to this seller")
        if job.status != JobStatus.QUOTED:
            raise HTTPException(status_code=409, detail=f"job is {job.status}, not quoted")
        job.status = JobStatus.MATCHED
    return {"status": "ok"}


@seller_router.post("/jobs/{job_id}/complete", response_model=Transaction)
def complete_job(job_id: UUID, seller_id: str = Depends(current_seller)) -> Transaction:
    with _LOCK:
        job = store.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.seller_id != seller_id:
            raise HTTPException(status_code=403, detail="job not assigned to this seller")
        if job.status != JobStatus.MATCHED:
            raise HTTPException(status_code=409, detail=f"job is {job.status}, not matched")
        if job.seller_payout is None:
            # Defensive: a MATCHED job always has a payout, but typing demands the check.
            raise HTTPException(status_code=500, detail="matched job missing payout")

        job.status = JobStatus.COMPLETED
        margin = round(job.buyer_price - job.seller_payout, 2)
        tx = Transaction(
            job_id=job.id,
            buyer_price=job.buyer_price,
            seller_payout=job.seller_payout,
            margin=margin,
        )
        store.record_transaction(tx)
        # Increment lifetime job counters so adjusters like new_buyer_discount work.
        store.get_or_create_buyer(job.buyer_id).completed_jobs += 1
        store.get_or_create_seller(seller_id).completed_jobs += 1
    return tx


# ---------- Admin router ----------

admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@admin_router.get("/config")
def get_config() -> dict[str, Any]:
    return config.to_dict()


@admin_router.put("/config/service_types/{service_type_id}", response_model=ServiceType)
def upsert_service_type(service_type_id: str, body: ServiceTypeBody) -> ServiceType:
    st = ServiceType(
        id=service_type_id,
        base_buyer_price=body.base_buyer_price,
        base_seller_payout=body.base_seller_payout,
    )
    config.service_types[service_type_id] = st
    return st


@admin_router.put("/config/pipelines/{service_type_id}")
def upsert_pipelines(service_type_id: str, body: PipelinesBody) -> dict[str, list[str]]:
    if service_type_id not in config.service_types:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    unknown = sorted({n for n in (*body.buyer, *body.seller) if n not in REGISTRY})
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown adjuster(s): {unknown}")
    pipelines = Pipelines(buyer=body.buyer, seller=body.seller)
    config.pipelines[service_type_id] = pipelines
    return {"buyer": pipelines.buyer, "seller": pipelines.seller}


@admin_router.put("/config/margin_floor")
def update_margin_floor(body: MarginFloorBody) -> dict[str, float]:
    config.margin_floor = MarginFloor(
        absolute=body.absolute,
        pct=body.pct,
        ceiling_multiplier=body.ceiling_multiplier,
    )
    return {
        "absolute": config.margin_floor.absolute,
        "pct": config.margin_floor.pct,
        "ceiling_multiplier": config.margin_floor.ceiling_multiplier,
    }


@admin_router.put("/config/matching_strategy")
def update_matching_strategy(body: MatchingStrategyBody) -> dict[str, str]:
    if body.strategy not in STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"strategy must be one of {sorted(STRATEGIES.keys())}",
        )
    config.matching_strategy = body.strategy
    return {"strategy": body.strategy}


@admin_router.put("/config/adjuster_params/{adjuster_name}")
def update_adjuster_params(adjuster_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Set parameters for a registered adjuster. Composing/tuning happens here.

    Values are bounded where they are read (see `pricing._bounded`), so an
    out-of-range param is clamped, not trusted.
    """
    if adjuster_name not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown adjuster {adjuster_name!r}")
    config.adjuster_params[adjuster_name] = body
    return body


@admin_router.get("/transactions", response_model=list[Transaction])
def list_transactions(
    limit: int = Query(default=100, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
) -> list[Transaction]:
    return store.transactions[offset : offset + limit]


@admin_router.get("/margins/summary")
def margins_summary() -> dict[str, float]:
    txs = store.transactions
    revenue = sum(t.buyer_price for t in txs)
    payouts = sum(t.seller_payout for t in txs)
    margin = sum(t.margin for t in txs)
    take_rate = (margin / revenue) if revenue > 0 else 0.0
    return {
        "transactions": float(len(txs)),
        "gross_revenue": revenue,
        "seller_payouts": payouts,
        "platform_margin": margin,
        "take_rate": round(take_rate, 4),
    }


# ---------- App assembly ----------

app = FastAPI(title="Marketplace", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Seller router is included first so `/jobs/offered` is matched before the
# buyer router's `/jobs/{job_id}` — otherwise "offered" tries to parse as a
# UUID and the request 422s.
app.include_router(seller_router)
app.include_router(buyer_router)
app.include_router(admin_router)
