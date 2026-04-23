"""FastAPI app.

Three routers:
  /...             buyer-facing
  /...             seller-facing
  /admin/...       operator-facing (the only routes that see both sides' numbers)

Buyer and seller responses use distinct view models that omit the other side's
price/payout — this is enforced by the model layer, not by hand-curated dict
keys. See `BuyerJobView` and `SellerJobView` in `models.py`.

Single global Config + Store instances. Module-level so tests can patch by
import. Reset between tests via `reset_state()`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException

from .config import Config, MarginFloor, Pipelines
from .matching import STRATEGIES, SellerCandidate
from .models import (
    AvailabilityRequest,
    BuyerJobView,
    Job,
    JobCreateRequest,
    JobStatus,
    Quote,
    QuoteRequest,
    SellerActionRequest,
    SellerJobView,
    ServiceType,
    Side,
    Transaction,
)
from .pricing import PricingContext, run_pipeline
from .store import Store

QUOTE_TTL = timedelta(minutes=5)


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
def create_quote(req: QuoteRequest) -> Quote:
    service_type = config.service_types.get(req.service_type_id)
    if service_type is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")

    supply = len(store.available_for(service_type.id))
    # Demand proxy: existing active jobs for this service + this fresh request.
    demand = store.active_demand(service_type.id) + 1

    buyer_price = _compute_buyer_price(service_type, req.buyer_id, supply, demand)
    probe_payout, _ = _probe_min_payout(service_type, supply, demand)

    if probe_payout is not None:
        floor = _floor_for(buyer_price)
        margin = buyer_price - probe_payout
        if margin < floor:
            # Bump buyer_price up to meet the floor.
            target = probe_payout + floor
            ceiling = service_type.base_buyer_price * config.margin_floor.ceiling_multiplier
            if target > ceiling:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"cannot quote: floor-corrected price {target:.2f} "
                        f"exceeds ceiling {ceiling:.2f} (base {service_type.base_buyer_price})"
                    ),
                )
            buyer_price = target

    quote = Quote(
        buyer_id=req.buyer_id,
        service_type_id=service_type.id,
        buyer_price=round(buyer_price, 2),
        expires_at=datetime.now(UTC) + QUOTE_TTL,
    )
    store.quotes[quote.id] = quote
    return quote


@buyer_router.post("/jobs", response_model=BuyerJobView)
def create_job(req: JobCreateRequest) -> BuyerJobView:
    quote = store.quotes.get(req.quote_id)
    if quote is None:
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
        raise HTTPException(status_code=422, detail="no available seller meets the margin floor")

    job.seller_id = result.seller_id
    job.seller_payout = round(result.seller_payout, 2)
    store.jobs[job.id] = job
    # Consume the quote — quotes are single-use.
    store.quotes.pop(quote.id, None)
    return BuyerJobView.from_job(job)


@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(job_id: UUID, role: str = "buyer") -> BuyerJobView:
    if role != "buyer":
        raise HTTPException(status_code=400, detail="this endpoint serves the buyer view only")
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return BuyerJobView.from_job(job)


# ---------- Seller router ----------

seller_router = APIRouter(tags=["seller"])


@seller_router.post("/availability")
def post_availability(req: AvailabilityRequest) -> dict[str, str]:
    if req.service_type_id not in config.service_types:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    store.add_availability(req.seller_id, req.service_type_id)
    return {"status": "ok"}


@seller_router.delete("/availability/{seller_id}/{service_type_id}")
def delete_availability(seller_id: str, service_type_id: str) -> dict[str, str]:
    removed = store.remove_availability(seller_id, service_type_id)
    if not removed:
        raise HTTPException(status_code=404, detail="availability not found")
    return {"status": "ok"}


@seller_router.get("/jobs/offered", response_model=list[SellerJobView])
def list_offered(seller_id: str) -> list[SellerJobView]:
    return [
        SellerJobView.from_job(j)
        for j in store.jobs.values()
        if j.seller_id == seller_id and j.status == JobStatus.QUOTED
    ]


@seller_router.post("/jobs/{job_id}/accept")
def accept_job(job_id: UUID, req: SellerActionRequest) -> dict[str, str]:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.seller_id != req.seller_id:
        raise HTTPException(status_code=403, detail="job not offered to this seller")
    if job.status != JobStatus.QUOTED:
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not quoted")
    job.status = JobStatus.MATCHED
    return {"status": "ok"}


@seller_router.post("/jobs/{job_id}/complete", response_model=Transaction)
def complete_job(job_id: UUID, req: SellerActionRequest) -> Transaction:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.seller_id != req.seller_id:
        raise HTTPException(status_code=403, detail="job not assigned to this seller")
    if job.status != JobStatus.MATCHED:
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not matched")
    if job.seller_payout is None:
        # Defensive: a MATCHED job always has a payout, but typing demands the check.
        raise HTTPException(status_code=500, detail="matched job missing payout")

    job.status = JobStatus.COMPLETED
    margin = job.buyer_price - job.seller_payout
    tx = Transaction(
        job_id=job.id,
        buyer_price=job.buyer_price,
        seller_payout=job.seller_payout,
        margin=margin,
    )
    store.record_transaction(tx)
    # Increment lifetime job counters so adjusters like new_buyer_discount work.
    store.get_or_create_buyer(job.buyer_id).completed_jobs += 1
    store.get_or_create_seller(req.seller_id).completed_jobs += 1
    return tx


# ---------- Admin router ----------

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/config")
def get_config() -> dict[str, Any]:
    return config.to_dict()


@admin_router.put("/config/service_types/{service_type_id}", response_model=ServiceType)
def upsert_service_type(service_type_id: str, body: dict[str, float]) -> ServiceType:
    if "base_buyer_price" not in body or "base_seller_payout" not in body:
        raise HTTPException(
            status_code=422, detail="body requires base_buyer_price and base_seller_payout"
        )
    st = ServiceType(
        id=service_type_id,
        base_buyer_price=float(body["base_buyer_price"]),
        base_seller_payout=float(body["base_seller_payout"]),
    )
    config.service_types[service_type_id] = st
    return st


@admin_router.put("/config/pipelines/{service_type_id}")
def upsert_pipelines(service_type_id: str, body: dict[str, list[str]]) -> dict[str, list[str]]:
    if service_type_id not in config.service_types:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    pipelines = Pipelines(buyer=body.get("buyer", []), seller=body.get("seller", []))
    config.pipelines[service_type_id] = pipelines
    return {"buyer": pipelines.buyer, "seller": pipelines.seller}


@admin_router.put("/config/margin_floor")
def update_margin_floor(body: dict[str, float]) -> dict[str, float]:
    config.margin_floor = MarginFloor(
        absolute=float(body.get("absolute", 0.0)),
        pct=float(body.get("pct", 0.0)),
        ceiling_multiplier=float(body.get("ceiling_multiplier", 3.0)),
    )
    return {
        "absolute": config.margin_floor.absolute,
        "pct": config.margin_floor.pct,
        "ceiling_multiplier": config.margin_floor.ceiling_multiplier,
    }


@admin_router.put("/config/matching_strategy")
def update_matching_strategy(body: dict[str, str]) -> dict[str, str]:
    name = body.get("strategy")
    if not name or name not in STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"strategy must be one of {sorted(STRATEGIES.keys())}",
        )
    config.matching_strategy = name
    return {"strategy": name}


@admin_router.put("/config/adjuster_params/{adjuster_name}")
def update_adjuster_params(adjuster_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Set parameters for a registered adjuster. Composing/tuning happens here."""
    from .pricing import REGISTRY

    if adjuster_name not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown adjuster {adjuster_name!r}")
    config.adjuster_params[adjuster_name] = body
    return body


@admin_router.get("/transactions", response_model=list[Transaction])
def list_transactions() -> list[Transaction]:
    return list(store.transactions)


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
# Seller router is included first so `/jobs/offered` is matched before the
# buyer router's `/jobs/{job_id}` — otherwise "offered" tries to parse as a
# UUID and the request 422s.
app.include_router(seller_router)
app.include_router(buyer_router)
app.include_router(admin_router)
