"""FastAPI app.

Routers, all under ``/v1`` and role-scoped so paths never collide:
  /v1/...          buyer-facing
  /v1/seller/...   seller-facing
  /v1/admin/...    operator-facing (the only routes that see both sides' numbers)

Identity comes from the authenticated principal (`auth.py`), never a request
body/query. State lives in Postgres (SQLite for local/tests) via a per-request
session (`db.get_session`); concurrency is handled by DB transactions and
row locks, not a process-wide lock. Buyer and seller responses use distinct view
models that omit the other side's number (`models.py`).
"""

import asyncio
import dataclasses
import logging
import math
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, notifications, repo
from .auth import (
    Claims,
    Principal,
    auth_router,
    bootstrap_admin,
    current_buyer,
    current_participant,
    current_seller,
    require_admin,
)
from .config import Candidate, PricingConfig
from .db import CommitRoute, SessionLocal, get_session, init_db
from .entities import (
    Adjustment,
    AuditLog,
    AuthSession,
    Availability,
    BuyerProfile,
    Dispute,
    EmailToken,
    IdempotencyRecord,
    Job,
    Listing,
    Notification,
    NotificationMute,
    Offer,
    Payment,
    Payout,
    Pipeline,
    Quote,
    Report,
    Review,
    SellerProfile,
    SellerReview,
    ServiceType,
    Transaction,
    User,
    WebhookEvent,
)
from .idempotency import IdempotencyMiddleware
from .mail import get_mail_sender
from .matching import STRATEGIES, estimated_fee, passes_floor, required_spread, seller_payout_for
from .models import (
    AdjustmentKind,
    AdminDisputeOut,
    AdminReportOut,
    AdminReviewOut,
    AdminSellerBody,
    AuditOut,
    AvailabilityRequest,
    BuyerDisputeOut,
    BuyerJobView,
    BuyerProfileOut,
    CatalogListingOut,
    CatalogSellerProfileOut,
    DisputeRequest,
    DisputeSource,
    DisputeStatus,
    EventKind,
    FeesBody,
    JobCreateRequest,
    JobReviewOut,
    JobStatus,
    ListingCreate,
    ListingUpdate,
    MarginFloorBody,
    MarginSummaryOut,
    MatchingStrategyBody,
    NotificationOut,
    NotificationPreferenceOut,
    NotificationPreferencesUpdate,
    NotificationStats,
    NotificationStatus,
    OfferStatus,
    OnboardingOut,
    PaymentStatus,
    PayoutOut,
    PayoutStatus,
    PipelinesBody,
    QuoteOut,
    QuoteRequest,
    ReportOut,
    ReportRequest,
    ReportStatus,
    ReportTargetKind,
    ResolveDisputeRequest,
    ResolveReportRequest,
    ReviewOut,
    ReviewRequest,
    SellerCompletionOut,
    SellerDisputeOut,
    SellerJobView,
    SellerListingOut,
    SellerOfferView,
    SellerProfileOut,
    SellerProfileUpdate,
    SellerReviewOut,
    ServiceTypeBody,
    ServiceTypeOut,
    Side,
    StatsOut,
    SuspendRequest,
    TransactionOut,
    UserModerationOut,
    UserRole,
    UserStatus,
    WebhookEventOut,
    WebhookEventStatus,
    to_money,
)
from .observability import BodySizeLimitMiddleware, RequestIdMiddleware, configure_logging
from .pages import pages_router
from .payments import get_provider
from .payments.port import PaymentError, PaymentEvent, PaymentProvider, WebhookSignatureError
from .pricing import REGISTRY, PricingContext, run_pipeline
from .repo import audit
from .settings import settings

logger = logging.getLogger("marketplace")

MAX_ACTIVE_QUOTES = 100_000  # ponytail: crude OOM backstop; real limits at the gateway.
MAX_PAGE = 200

SessionDep = Annotated[Session, Depends(get_session)]
BuyerId = Annotated[str, Depends(current_buyer)]
SellerId = Annotated[str, Depends(current_seller)]
AdminId = Annotated[str, Depends(require_admin)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0)]
ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]


def _now() -> datetime:
    return datetime.now(UTC)


_STARTED_AT = _now()


def _require_active(session: Session, user_id: str) -> None:
    """Verb gate for suspension: acquisition endpoints call this first.
    A missing row is treated as active (auth already proved the principal)."""
    user = session.get(User, user_id)
    if user is not None and user.status is UserStatus.SUSPENDED:
        raise HTTPException(status_code=403, detail="account suspended")


# ---------- Matching / offer helpers ----------


def _create_offer(session: Session, job: Job, seller_id: str, payout: Any) -> None:
    expires_at = _now() + timedelta(minutes=settings.offer_ttl_minutes)
    session.add(
        Offer(
            job_id=job.id,
            service_type_id=job.service_type_id,
            seller_id=seller_id,
            seller_payout=payout,
            expires_at=expires_at,
        )
    )
    notifications.enqueue(
        session,
        EventKind.OFFER_RECEIVED,
        seller_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "seller_payout": str(payout),
            "expires_at": expires_at.isoformat(),
        },
    )


def _expire_unmatched(session: Session, job: Job) -> None:
    job.status = JobStatus.EXPIRED
    notifications.enqueue(
        session,
        EventKind.JOB_EXPIRED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "reason": "no seller available",
        },
    )


def _match_and_offer(session: Session, job: Job) -> None:
    """Offer `job` to the next eligible seller, or mark it EXPIRED if none fit.

    Excludes any seller who already had an offer for this job (so decline/expiry
    walk down the candidate list instead of looping).
    """
    if job.pinned_seller_id is not None:
        # Browse contract: the buyer chose this seller — never substitute.
        # Pinned jobs get their offer at creation; a re-match call means
        # decline/expiry, so the job ends here.
        _expire_unmatched(session, job)
        return
    cfg = repo.load_pricing_config(session, job.service_type_id)
    if cfg is None:
        _expire_unmatched(session, job)
        return
    seen = repo.sellers_seen_for_job(session, job.id)
    candidates = repo.eligible_candidates(session, job.service_type_id, seen)
    supply = repo.available_count(session, job.service_type_id)
    demand = repo.active_demand(session, job.service_type_id)
    strategy = STRATEGIES.get(cfg.matching_strategy)
    result = strategy(job.buyer_price, candidates, cfg, supply, demand) if strategy else None
    if result is None:
        _expire_unmatched(session, job)
    else:
        _create_offer(session, job, result.seller_id, result.seller_payout)


def _sweep_expired_offers(session: Session) -> None:
    """Lazy sweep: expire timed-out offers and re-match their jobs. Called on reads."""
    stale = session.scalars(
        select(Offer).where(Offer.status == OfferStatus.OFFERED, Offer.expires_at < _now())
    ).all()
    for offer in stale:
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()
        job = session.get(Job, offer.job_id)
        if job is not None and job.status == JobStatus.PENDING:
            _match_and_offer(session, job)


def _sweep_stale_payments(session: Session, provider: PaymentProvider) -> None:
    """Jobs stuck AWAITING_PAYMENT past the TTL expire and free the seller's slot."""
    deadline = _now() - timedelta(minutes=settings.payment_ttl_minutes)
    stale = session.scalars(
        select(Job).where(Job.status == JobStatus.AWAITING_PAYMENT, Job.accepted_at < deadline)
    ).all()
    for job in stale:
        # Re-lock (payment first, then job — same order as _apply_payment_event)
        # and re-check: a concurrent payment_succeeded webhook may have accepted
        # this job between the unlocked select above and now. Never expire a paid job.
        payment = session.scalar(select(Payment).where(Payment.job_id == job.id).with_for_update())
        # populate_existing: the unlocked select() above cached this Job; the
        # locked re-get must return the row's CURRENT status, not the cached
        # one, or the "never expire a paid job" guard reads stale state (F5).
        locked_job = session.get(Job, job.id, with_for_update=True, populate_existing=True)
        if locked_job is None or locked_job.status != JobStatus.AWAITING_PAYMENT:
            continue
        if payment is not None:
            if payment.status is PaymentStatus.SUCCEEDED:
                continue  # paid — the webhook owns this job's fate, not the sweep
            try:
                provider.cancel_charge(payment.provider_payment_id)
            except PaymentError as exc:
                logger.warning(
                    "void failed for payment %s (job %s), will retry next sweep: %s",
                    payment.provider_payment_id,
                    job.id,
                    exc,
                )
                continue  # provider hiccup: leave it; the next sweep retries
            payment.status = PaymentStatus.FAILED
        locked_job.status = JobStatus.EXPIRED
        notifications.enqueue(
            session,
            EventKind.JOB_EXPIRED_BUYER,
            locked_job.buyer_id,
            {
                "job_id": str(locked_job.id),
                "service_type_id": locked_job.service_type_id,
                "reason": "payment window elapsed",
            },
        )


def _sweep_expired_auth(session: Session) -> None:
    """Expired sessions and email tokens are dead weight — drop them on reads."""
    session.execute(delete(AuthSession).where(AuthSession.expires_at < _now()))
    session.execute(delete(EmailToken).where(EmailToken.expires_at < _now()))


def _sweep_retention(session: Session) -> None:
    """Bounded tables: reap replay/dedup rows and delivered mail past their
    windows. PENDING outbox rows are immortal — the outbox contract holds —
    and so are FAILED webhook rows: they are still owed a retry."""
    now = _now()
    session.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.created_at < now - timedelta(days=settings.retention_idempotency_days)
        )
    )
    session.execute(
        delete(WebhookEvent).where(
            WebhookEvent.status != WebhookEventStatus.FAILED,
            WebhookEvent.received_at < now - timedelta(days=settings.retention_webhooks_days),
        )
    )
    session.execute(
        delete(Notification).where(
            Notification.status.in_((NotificationStatus.SENT, NotificationStatus.FAILED)),
            Notification.created_at < now - timedelta(days=settings.retention_notifications_days),
        )
    )


def _sweep(session: Session, provider: PaymentProvider) -> None:
    """Everything lazy maintenance does on reads: offers, payments, auth."""
    _sweep_expired_offers(session)
    _sweep_stale_payments(session, provider)
    _sweep_expired_auth(session)
    _sweep_retention(session)


def _run_drain_once() -> None:
    """One outbox drain pass on a worker thread (sync Session stays off the loop)."""
    notifications.drain_once(get_mail_sender())


def _run_sweep_once() -> None:
    with SessionLocal() as session:
        _sweep(session, get_provider())
        session.commit()


async def _maintenance_loop() -> None:
    """The template's heartbeat: drain the outbox every few seconds and run the
    sweeps every minute, so offers/payments/sessions expire — and sellers get
    their 2-minute-TTL offer emails — even when no requests arrive. Ticks are
    crash-proof; cancellation (lifespan shutdown) stops the loop."""
    last_sweep = time.monotonic()
    while True:
        await asyncio.sleep(settings.notify_drain_seconds)
        try:
            await asyncio.to_thread(_run_drain_once)
        except Exception:
            logger.exception("notification drain tick failed")
        if time.monotonic() - last_sweep >= settings.sweep_interval_seconds:
            last_sweep = time.monotonic()
            try:
                await asyncio.to_thread(_run_sweep_once)
            except Exception:
                logger.exception("sweep tick failed")
            try:
                await asyncio.to_thread(_run_webhook_retry_once)
            except Exception:
                logger.exception("webhook retry tick failed")


def _paginate[T](rows: Sequence[T], limit: int, offset: int) -> list[T]:
    return list(rows[offset : offset + limit])


def _count_by(session: Session, column: Any, enum: type[StrEnum]) -> dict[str, int]:
    """Group-count a status column; every enum member present, absent = 0."""
    rows: dict[Any, int] = {}
    for value, count in session.execute(select(column, func.count()).group_by(column)).all():
        rows[value] = count
    return {m.value: int(rows.get(m, 0)) for m in enum}


# ---------- Reports router ----------

reports_router = APIRouter(prefix="/v1", tags=["reports"], route_class=CommitRoute)
ParticipantClaims = Annotated[Claims, Depends(current_participant)]


@reports_router.post("/reports", response_model=ReportOut, status_code=201)
def file_report(body: ReportRequest, session: SessionDep, claims: ParticipantClaims) -> Report:
    reporter_id = claims.sub
    _require_active(session, reporter_id)
    target_id = body.target_id
    if body.target_kind is ReportTargetKind.USER:
        target = session.get(User, body.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="target not found")
        if target.id == reporter_id:
            raise HTTPException(status_code=422, detail="cannot report yourself")
        shared = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                or_(
                    and_(Job.buyer_id == reporter_id, Job.seller_id == target.id),
                    and_(Job.seller_id == reporter_id, Job.buyer_id == target.id),
                )
            )
        )
        if not shared:
            raise HTTPException(status_code=403, detail="not a counterparty")
    else:
        model = Review if body.target_kind is ReportTargetKind.REVIEW else SellerReview
        try:
            review_uuid = UUID(body.target_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="target not found") from None
        row = session.get(model, review_uuid)
        if row is None:
            raise HTTPException(status_code=404, detail="target not found")
        if reporter_id not in (row.buyer_id, row.seller_id):
            raise HTTPException(status_code=403, detail="not a party to this review")
        # Canonicalize: UUID() accepts many textual forms of the same value
        # (case, hyphens, braces, urn:uuid:); storing the raw string would let
        # each variant evade the (reporter, target) UNIQUE and re-notify admins.
        target_id = str(review_uuid)

    report = Report(
        reporter_id=reporter_id,
        target_kind=body.target_kind,
        target_id=target_id,
        reason=body.reason,
    )
    session.add(report)
    try:
        session.flush()
    except IntegrityError:
        # Duplicate (sequential or concurrent) loses the UNIQUE race — same
        # answer either way, not a 500.
        raise HTTPException(status_code=409, detail="already reported") from None
    notifications.enqueue_admins(
        session,
        EventKind.REPORT_OPENED_ADMIN,
        {
            "report_id": str(report.id),
            "target_kind": body.target_kind,
            "target_id": target_id,
            "reason": body.reason,
            "reporter_id": reporter_id,
        },
    )
    return report


@reports_router.get("/reports", response_model=list[ReportOut])
def my_reports(session: SessionDep, claims: ParticipantClaims) -> list[Report]:
    return list(
        session.scalars(
            select(Report)
            .where(Report.reporter_id == claims.sub)
            .order_by(Report.created_at.desc())
        ).all()
    )


# ---------- Notification preferences router ----------

prefs_router = APIRouter(prefix="/v1", tags=["preferences"], route_class=CommitRoute)


def _pref_rows(session: Session, claims: Claims) -> list[NotificationPreferenceOut]:
    kinds = [k for k, role in notifications.KIND_ROLES.items() if role is claims.role]
    muted = set(
        session.scalars(
            select(NotificationMute.kind).where(NotificationMute.user_id == claims.sub)
        ).all()
    )
    return [
        NotificationPreferenceOut(
            kind=k,
            muted=k in muted and k not in notifications.MUST_SEND,
            locked=k in notifications.MUST_SEND,
        )
        for k in kinds
    ]


@prefs_router.get("/notification-preferences", response_model=list[NotificationPreferenceOut])
def get_notification_preferences(
    session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return _pref_rows(session, claims)


@prefs_router.put("/notification-preferences", response_model=list[NotificationPreferenceOut])
def put_notification_preferences(
    body: NotificationPreferencesUpdate, session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    wanted = set(body.muted)
    for kind in wanted:
        if kind in notifications.MUST_SEND:
            raise HTTPException(status_code=422, detail=f"{kind} cannot be muted")
        if notifications.KIND_ROLES[kind] is not claims.role:
            raise HTTPException(status_code=422, detail=f"{kind} is not a {claims.role} kind")
    # Serialize concurrent PUTs for the same user: naive delete-then-insert
    # under READ COMMITTED unions both sets (each DELETE misses the other's
    # uncommitted rows). The row lock makes replace-set last-writer-wins.
    session.get(User, claims.sub, with_for_update=True)
    session.execute(delete(NotificationMute).where(NotificationMute.user_id == claims.sub))
    for kind in sorted(wanted):
        session.add(NotificationMute(user_id=claims.sub, kind=kind))
    try:
        session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="preferences changed concurrently, retry"
        ) from None
    return _pref_rows(session, claims)


# ---------- Buyer router ----------


def _buyer_view(session: Session, job: Job) -> BuyerJobView:
    """BuyerJobView plus the buyer's payment state (never the seller's numbers)."""
    view = BuyerJobView.model_validate(job)
    payment = session.scalar(select(Payment).where(Payment.job_id == job.id))
    if payment is not None:
        view.payment_status = payment.status
        if job.status == JobStatus.AWAITING_PAYMENT:
            view.client_secret = payment.client_secret
    return view


def _job_reviews(session: Session, job_id: UUID) -> list[JobReviewOut]:
    """Both reviews for a job (0-2), explicitly constructed — no party ids,
    and `comment` reads through `public_comment` so a hidden comment is null
    even to the job's own parties."""
    out: list[JobReviewOut] = []
    review = session.scalar(select(Review).where(Review.job_id == job_id))
    if review is not None:
        out.append(
            JobReviewOut(
                id=review.id,
                kind=ReportTargetKind.REVIEW.value,
                rating=review.rating,
                comment=review.public_comment,
                created_at=review.created_at,
            )
        )
    seller_review = session.scalar(select(SellerReview).where(SellerReview.job_id == job_id))
    if seller_review is not None:
        out.append(
            JobReviewOut(
                id=seller_review.id,
                kind=ReportTargetKind.SELLER_REVIEW.value,
                rating=seller_review.rating,
                comment=seller_review.public_comment,
                created_at=seller_review.created_at,
            )
        )
    return out


buyer_router = APIRouter(prefix="/v1", tags=["buyer"], route_class=CommitRoute)

# Moved to catalog.py (shared with the HTML pages layer); re-exported here
# under their original names — tests/test_catalog.py imports the first two
# directly from marketplace.api, and every call site below is unchanged.
_indicative_buyer_price = catalog.indicative_buyer_price
_listing_bookable = catalog.listing_bookable
_visible_listings_stmt = catalog.visible_listings_stmt
_catalog_seller_out = catalog.catalog_seller_out
_catalog_listing_out = catalog.catalog_listing_out
_catalog_listings_for_rows = catalog.catalog_listings_for_rows


@buyer_router.post("/quotes", response_model=QuoteOut, status_code=201)
def create_quote(req: QuoteRequest, session: SessionDep, buyer_id: BuyerId) -> Quote:
    _require_active(session, buyer_id)
    listing: Listing | None = None
    if req.listing_id is not None:
        listing = session.get(Listing, req.listing_id)
        if listing is None or not _listing_bookable(session, listing):
            raise HTTPException(status_code=409, detail="listing unavailable")
        # I1: a listing whose seller has NO Availability row is otherwise
        # fully visible/bookable (`_listing_bookable` never checks
        # Availability) -- every booking against it would consume the quote
        # and 410 at job time with nothing to fall back to. Reject here,
        # before anything is consumed, instead of at job time.
        if (
            session.scalar(
                select(Availability).where(
                    Availability.seller_id == listing.seller_id,
                    Availability.service_type_id == listing.service_type_id,
                )
            )
            is None
        ):
            raise HTTPException(status_code=409, detail="listing unavailable")
        if req.service_type_id is not None and req.service_type_id != listing.service_type_id:
            raise HTTPException(status_code=422, detail="listing_id and service_type_id disagree")
        service_type_id = listing.service_type_id
    elif req.service_type_id is not None:
        service_type_id = req.service_type_id
    else:
        raise HTTPException(status_code=422, detail="listing_id or service_type_id required")
    cfg = repo.load_pricing_config(session, service_type_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")

    session.execute(delete(Quote).where(Quote.expires_at < _now()))
    if (session.scalar(select(func.count()).select_from(Quote)) or 0) >= MAX_ACTIVE_QUOTES:
        raise HTTPException(status_code=503, detail="quote capacity reached, retry shortly")

    supply = repo.available_count(session, cfg.service.id)
    demand = repo.active_demand(session, cfg.service.id) + 1
    buyer = repo.get_or_create_buyer(session, buyer_id)

    # Cross-reference: mirrors _indicative_buyer_price (catalog's pre-floor
    # "from" price) above — keep both computations in sync.
    ctx = PricingContext(
        side=Side.BUYER,
        buyer_completed_jobs=buyer.completed_jobs,
        live_supply=supply,
        live_demand=demand,
    )
    buyer_price = to_money(
        run_pipeline(
            float(cfg.service.base_buyer_price), cfg.buyer_pipeline, ctx, cfg.adjuster_params
        )
    )

    if listing is not None:
        # Pinned: probe the CHOSEN seller's payout, not the market minimum —
        # the buyer picked this seller, so the floor check must reflect what
        # the platform would actually pay them, not whoever is cheapest.
        pinned_candidate = repo.candidate_for_seller(session, listing.seller_id)
        if pinned_candidate is None:
            raise HTTPException(status_code=409, detail="listing unavailable")
        probe = seller_payout_for(pinned_candidate, cfg, supply, demand)
    else:
        payouts = [
            seller_payout_for(c, cfg, supply, demand)
            for c in repo.eligible_candidates(session, cfg.service.id, set())
        ]
        probe = min(payouts) if payouts else None
    if probe is not None:
        required = required_spread(buyer_price, cfg.margin_floor, cfg.fees)
        if buyer_price - probe < required:
            # Round the corrected price UP to a whole unit so it isn't pinned to
            # exactly probe + required (which would leak the seller's payout).
            # Both the pct floor and the fee grow with the bumped price, so one
            # ceil can undershoot — walk whole units until the invariant holds,
            # bounded by the ceiling.
            ceiling = to_money(cfg.service.base_buyer_price * cfg.margin_floor.ceiling_multiplier)
            target = to_money(math.ceil(probe + required))
            while target <= ceiling and target - probe < required_spread(
                target, cfg.margin_floor, cfg.fees
            ):
                target += 1
            if target > ceiling:
                raise HTTPException(
                    status_code=422,
                    detail="cannot quote: required margin exceeds the configured price ceiling",
                )
            buyer_price = target

    quote = Quote(
        buyer_id=buyer_id,
        service_type_id=cfg.service.id,
        buyer_price=buyer_price,
        expires_at=_now() + timedelta(minutes=settings.quote_ttl_minutes),
        listing_id=req.listing_id,
        pinned_seller_id=(listing.seller_id if listing is not None else None),
    )
    session.add(quote)
    session.flush()
    return quote


@buyer_router.post("/jobs", response_model=BuyerJobView, status_code=201)
def create_job(req: JobCreateRequest, session: SessionDep, buyer_id: BuyerId) -> Job:
    _require_active(session, buyer_id)
    quote = session.get(Quote, req.quote_id, with_for_update=True)
    # Same 404 whether missing or not-yours — don't confirm someone else's quote.
    if quote is None or quote.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="quote not found")
    if quote.expires_at < _now():
        raise HTTPException(status_code=410, detail="quote expired")

    cfg: PricingConfig | None = None
    payout_candidate: Candidate | None = None
    payout: Decimal | None = None
    if quote.pinned_seller_id is not None:
        listing = session.get(Listing, quote.listing_id) if quote.listing_id else None
        has_availability = (
            session.scalar(
                select(Availability).where(
                    Availability.seller_id == quote.pinned_seller_id,
                    Availability.service_type_id == quote.service_type_id,
                )
            )
            is not None
        )
        cfg = repo.load_pricing_config(session, quote.service_type_id)
        payout_candidate = repo.candidate_for_seller(session, quote.pinned_seller_id)
        if (
            listing is None
            or not _listing_bookable(session, listing)
            or not has_availability
            or cfg is None
            or payout_candidate is None
        ):
            # Single-use regardless of booking outcome — the same contract as
            # the unpinned no-seller-found case, which still consumes the
            # quote and simply yields an EXPIRED job. A pinned failure has no
            # rival to fall back to, so it 410s instead of returning a job;
            # commit the consumption here because CommitRoute never persists
            # a 4xx response, and a dead quote must not be retryable.
            session.delete(quote)
            session.commit()
            raise HTTPException(status_code=410, detail="listing no longer available")
        # C1: the quote-time probe checked the floor against the pinned
        # seller's payout AS OF THE QUOTE. Tier/pricing can drift inside the
        # quote's TTL window, so recompute the payout at JOB time here — before
        # the Job row is built — and re-check the floor. Compute once and
        # reuse below for `_create_offer` so the offer reflects the same
        # payout the check approved.
        supply = repo.available_count(session, quote.service_type_id)
        demand = repo.active_demand(session, quote.service_type_id)
        payout = seller_payout_for(payout_candidate, cfg, supply, demand)
        if not passes_floor(quote.buyer_price, payout, cfg.margin_floor, cfg.fees):
            # Same mid-commit-consume 410 contract as the guards above — never
            # leak payout/floor numbers in the response.
            session.delete(quote)
            session.commit()
            raise HTTPException(status_code=410, detail="listing no longer available")

    job = Job(
        quote_id=quote.id,
        buyer_id=quote.buyer_id,
        service_type_id=quote.service_type_id,
        buyer_price=quote.buyer_price,
    )
    session.add(job)
    session.delete(quote)  # quotes are single-use; deleting also blocks a racing reuse
    session.flush()
    if quote.pinned_seller_id is not None:
        assert cfg is not None and payout_candidate is not None and payout is not None  # guarded
        job.pinned_seller_id = quote.pinned_seller_id
        _create_offer(session, job, quote.pinned_seller_id, payout)
    else:
        _match_and_offer(session, job)  # PENDING with an offer, or EXPIRED if no seller fits
    session.flush()
    return job


@buyer_router.get("/jobs", response_model=list[BuyerJobView])
def list_buyer_jobs(
    session: SessionDep,
    buyer_id: BuyerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    stmt = select(Job).where(Job.buyer_id == buyer_id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    rows = session.scalars(stmt.order_by(Job.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep
) -> BuyerJobView:
    _sweep(session, provider)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _buyer_view(session, job)


@buyer_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def cancel_job(job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep) -> Job:
    job = session.get(Job, job_id)  # unlocked: existence + ownership only
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    # Canonical lock order is Payment → Job everywhere (_apply_payment_event,
    # _sweep_stale_payments). Locking Job first here would ABBA-deadlock against
    # a racing webhook on Postgres; _release_payment re-selects this same locked
    # Payment row inside the same transaction, which is fine.
    session.scalar(select(Payment).where(Payment.job_id == job_id).with_for_update())
    # populate_existing: discard the cached attributes from the unlocked
    # ownership peek above, so the status guard reads the row as it is UNDER
    # the lock, not as it was before a racing accept/complete (finding F5).
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in (JobStatus.PENDING, JobStatus.AWAITING_PAYMENT):
        raise HTTPException(status_code=409, detail=f"cannot cancel a {job.status} job")
    _expire_open_offers(session, job.id)
    try:
        refunded = _release_payment(session, provider, job)
    except PaymentError as exc:
        logger.warning("payment release failed for job %s: %s", job.id, exc)
        raise HTTPException(status_code=502, detail="payment provider unavailable, retry") from None
    _notify_cancelled(session, job, refunded)
    job.status = JobStatus.CANCELLED
    return job


def _notify_cancelled(session: Session, job: Job, refunded: bool) -> None:
    """Cancel notices: the committed seller always hears; the buyer only gets a
    receipt when money actually moved back (never a notice of their own action)."""
    if job.seller_id is not None:
        notifications.enqueue(
            session,
            EventKind.JOB_CANCELLED_SELLER,
            job.seller_id,
            {
                "job_id": str(job.id),
                "service_type_id": job.service_type_id,
                "seller_payout": str(job.seller_payout),
            },
        )
    if refunded:
        notifications.enqueue(
            session,
            EventKind.REFUND_ISSUED_BUYER,
            job.buyer_id,
            {"job_id": str(job.id), "buyer_price": str(job.buyer_price)},
        )


@buyer_router.get("/profile", response_model=BuyerProfileOut)
def get_buyer_profile(session: SessionDep, buyer_id: BuyerId) -> BuyerProfile:
    return repo.get_or_create_buyer(session, buyer_id)


@buyer_router.post("/jobs/{job_id}/review", response_model=ReviewOut, status_code=201)
def review_job(job_id: UUID, body: ReviewRequest, session: SessionDep, buyer_id: BuyerId) -> Review:
    _require_active(session, buyer_id)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.COMPLETED or job.seller_id is None:
        raise HTTPException(status_code=409, detail="can only review a completed job")
    if session.scalar(select(Review).where(Review.job_id == job_id)) is not None:
        raise HTTPException(status_code=409, detail="job already reviewed")

    seller = repo.get_or_create_seller(session, job.seller_id)
    seller.rating_count += 1
    seller.rating_sum += body.rating
    # get_or_create_seller's session.get() autoflushes any pending INSERT, so
    # the review is added only after it — otherwise the UNIQUE(job_id)
    # violation would surface uncaught by that autoflush, not by the guarded
    # flush() below.
    review = Review(
        job_id=job.id,
        buyer_id=buyer_id,
        seller_id=job.seller_id,
        rating=body.rating,
        comment=body.comment,
    )
    session.add(review)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise HTTPException(status_code=409, detail="job already reviewed") from None
    return review


@buyer_router.post("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut, status_code=201)
def open_dispute(
    job_id: UUID, body: DisputeRequest, session: SessionDep, buyer_id: BuyerId
) -> Dispute:
    _require_active(session, buyer_id)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.COMPLETED or job.completed_at is None:
        raise HTTPException(status_code=409, detail="only completed jobs can be disputed")
    if _now() > job.completed_at + timedelta(days=settings.dispute_window_days):
        raise HTTPException(status_code=409, detail="dispute window has elapsed")
    if session.scalar(select(Dispute).where(Dispute.job_id == job_id)) is not None:
        raise HTTPException(status_code=409, detail="job already disputed")

    dispute = Dispute(
        job_id=job.id, source=DisputeSource.BUYER, buyer_id=buyer_id, reason=body.reason
    )
    session.add(dispute)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise HTTPException(status_code=409, detail="job already disputed") from None
    if job.seller_id is not None:
        notifications.enqueue(
            session,
            EventKind.DISPUTE_OPENED_SELLER,
            job.seller_id,
            {
                "job_id": str(job.id),
                "service_type_id": job.service_type_id,
                "reason": body.reason,
            },
        )
    notifications.enqueue_admins(
        session,
        EventKind.DISPUTE_OPENED_ADMIN,
        {"job_id": str(job.id), "dispute_id": str(dispute.id), "reason": body.reason},
    )
    return dispute


@buyer_router.get("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut)
def get_dispute_buyer(job_id: UUID, session: SessionDep, buyer_id: BuyerId) -> Dispute:
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    dispute = session.scalar(select(Dispute).where(Dispute.job_id == job_id))
    if dispute is None:
        raise HTTPException(status_code=404, detail="no dispute for this job")
    return dispute


@buyer_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId
) -> list[JobReviewOut]:
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_reviews(session, job_id)


# ---------- Seller router ----------

seller_router = APIRouter(prefix="/v1/seller", tags=["seller"], route_class=CommitRoute)


def _expire_open_offers(session: Session, job_id: UUID) -> None:
    for offer in session.scalars(
        select(Offer).where(Offer.job_id == job_id, Offer.status == OfferStatus.OFFERED)
    ).all():
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()


def _release_payment(session: Session, provider: PaymentProvider, job: Job) -> bool:
    """Undo whatever the job's charge collected: void a pending PI, refund a
    succeeded one. No-op when nothing was charged. Raises PaymentError upward.
    Returns True when a refund was issued, so callers can notify the buyer."""
    payment = session.scalar(select(Payment).where(Payment.job_id == job.id).with_for_update())
    if payment is None:
        return False
    if payment.status is PaymentStatus.SUCCEEDED:
        provider.refund(payment.provider_payment_id, idempotency_key=f"refund:{job.id}")
        payment.status = PaymentStatus.REFUNDED
        return True
    if payment.status is PaymentStatus.PENDING:
        provider.cancel_charge(payment.provider_payment_id)
        payment.status = (
            PaymentStatus.FAILED
        )  # ponytail: voided lands in FAILED, split if ops needs it
    return False


@seller_router.put("/profile", response_model=SellerProfileOut)
def update_profile(
    body: SellerProfileUpdate, session: SessionDep, seller_id: SellerId
) -> SellerProfile:
    seller = repo.get_or_create_seller(session, seller_id)
    seller.capacity = body.capacity
    session.flush()
    return seller


@seller_router.get("/profile", response_model=SellerProfileOut)
def get_profile(session: SessionDep, seller_id: SellerId) -> SellerProfile:
    return repo.get_or_create_seller(session, seller_id)


@seller_router.post("/payments/onboard", response_model=OnboardingOut)
def onboard_payments(
    session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> OnboardingOut:
    """Create the seller's payment account (once) and return the onboarding link.

    `payments_ready` flips via the provider's account webhook (instantly for the
    fake provider); matching only offers jobs to ready sellers."""
    _require_active(session, seller_id)
    seller = repo.get_or_create_seller(session, seller_id)
    if seller.provider_account_id is None:
        try:
            acct = provider.create_seller_account(seller_id, idempotency_key=f"acct:{seller_id}")
        except PaymentError as exc:
            logger.warning("account creation failed for seller %s: %s", seller_id, exc)
            raise HTTPException(
                status_code=502, detail="payment provider unavailable, retry"
            ) from None
        seller.provider_account_id = acct.provider_account_id
        seller.payments_ready = acct.payments_ready
        session.flush()
    return OnboardingOut(
        onboarding_url=provider.onboarding_link(
            seller.provider_account_id, settings.onboarding_return_url
        ),
        payments_ready=seller.payments_ready,
    )


@seller_router.post("/availability")
def post_availability(
    req: AvailabilityRequest, session: SessionDep, seller_id: SellerId
) -> dict[str, str]:
    _require_active(session, seller_id)
    if session.get(ServiceType, req.service_type_id) is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    repo.get_or_create_seller(session, seller_id)
    existing = session.scalar(
        select(Availability).where(
            Availability.seller_id == seller_id,
            Availability.service_type_id == req.service_type_id,
        )
    )
    if existing is None:
        session.add(Availability(seller_id=seller_id, service_type_id=req.service_type_id))
    return {"status": "ok"}


@seller_router.delete("/availability/{service_type_id}")
def delete_availability(
    service_type_id: str, session: SessionDep, seller_id: SellerId
) -> dict[str, str]:
    row = session.scalar(
        select(Availability).where(
            Availability.seller_id == seller_id,
            Availability.service_type_id == service_type_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="availability not found")
    session.delete(row)
    return {"status": "ok"}


@seller_router.get("/offers", response_model=list[SellerOfferView])
def list_offers(
    session: SessionDep,
    seller_id: SellerId,
    provider: ProviderDep,
    status: OfferStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Offer]:
    _sweep(session, provider)
    stmt = select(Offer).where(Offer.seller_id == seller_id)
    if status is not None:
        stmt = stmt.where(Offer.status == status)
    else:
        stmt = stmt.where(Offer.status == OfferStatus.OFFERED)
    rows = session.scalars(stmt.order_by(Offer.offered_at.desc())).all()
    return _paginate(rows, limit, offset)


@seller_router.get("/jobs", response_model=list[SellerJobView])
def list_seller_jobs(
    session: SessionDep,
    seller_id: SellerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    stmt = select(Job).where(Job.seller_id == seller_id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    rows = session.scalars(stmt.order_by(Job.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@seller_router.get("/jobs/{job_id}/dispute", response_model=SellerDisputeOut)
def get_dispute_seller(job_id: UUID, session: SessionDep, seller_id: SellerId) -> Dispute:
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="job not found")
    dispute = session.scalar(select(Dispute).where(Dispute.job_id == job_id))
    if dispute is None:
        raise HTTPException(status_code=404, detail="no dispute for this job")
    return dispute


@seller_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_seller(
    job_id: UUID, session: SessionDep, seller_id: SellerId
) -> list[JobReviewOut]:
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_reviews(session, job_id)


@seller_router.post("/jobs/{job_id}/review", response_model=SellerReviewOut, status_code=201)
def review_buyer(
    job_id: UUID, body: ReviewRequest, session: SessionDep, seller_id: SellerId
) -> SellerReview:
    _require_active(session, seller_id)
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="can only review a completed job")
    if session.scalar(select(SellerReview).where(SellerReview.job_id == job_id)) is not None:
        raise HTTPException(status_code=409, detail="job already reviewed")

    buyer = repo.get_or_create_buyer(session, job.buyer_id)
    buyer.rating_count += 1
    buyer.rating_sum += body.rating
    # get_or_create_buyer's session.get() autoflushes any pending INSERT, so
    # the review is added only after it — otherwise the UNIQUE(job_id)
    # violation would surface uncaught by that autoflush, not by the guarded
    # flush() below.
    review = SellerReview(
        job_id=job.id,
        seller_id=seller_id,
        buyer_id=job.buyer_id,
        rating=body.rating,
        comment=body.comment,
    )
    session.add(review)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise HTTPException(status_code=409, detail="job already reviewed") from None
    return review


@seller_router.post("/listings", response_model=SellerListingOut, status_code=201)
def create_listing(body: ListingCreate, session: SessionDep, seller_id: SellerId) -> Listing:
    _require_active(session, seller_id)
    if session.get(ServiceType, body.service_type_id) is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    seller = repo.get_or_create_seller(session, seller_id)
    if not seller.payments_ready:
        raise HTTPException(status_code=409, detail="onboard payments before listing")
    listing = Listing(
        seller_id=seller_id,
        service_type_id=body.service_type_id,
        title=body.title,
        description=body.description,
        photo_urls=body.photo_urls,
    )
    session.add(listing)
    session.flush()
    return listing


@seller_router.get("/listings", response_model=list[SellerListingOut])
def my_listings(session: SessionDep, seller_id: SellerId) -> list[Listing]:
    rows = session.scalars(
        select(Listing).where(Listing.seller_id == seller_id).order_by(Listing.created_at.desc())
    ).all()
    return list(rows)


@seller_router.put("/listings/{listing_id}", response_model=SellerListingOut)
def update_listing(
    listing_id: UUID, body: ListingUpdate, session: SessionDep, seller_id: SellerId
) -> Listing:
    _require_active(session, seller_id)
    listing = session.get(Listing, listing_id)
    if listing is None or listing.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="listing not found")
    for field in ("title", "description", "photo_urls", "active"):
        value = getattr(body, field)
        if value is not None:
            setattr(listing, field, value)
    session.flush()
    return listing


@seller_router.post("/offers/{offer_id}/accept", response_model=SellerJobView)
def accept_offer(
    offer_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Job:
    _require_active(session, seller_id)
    offer = session.get(Offer, offer_id, with_for_update=True)
    if offer is None or offer.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="offer not found")
    if offer.status != OfferStatus.OFFERED:
        raise HTTPException(status_code=409, detail=f"offer is {offer.status}, not open")
    if offer.expires_at < _now():
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()
        raise HTTPException(status_code=410, detail="offer expired")

    # Lock the seller row so two concurrent accepts can't exceed capacity.
    seller = session.get(SellerProfile, seller_id, with_for_update=True)
    if seller is None:
        seller = repo.get_or_create_seller(session, seller_id)
    if repo.active_job_count(session, seller_id) >= seller.capacity:
        raise HTTPException(status_code=409, detail="at capacity — complete a job first")

    job = session.get(Job, offer.job_id, with_for_update=True)
    if job is None or job.status != JobStatus.PENDING:
        raise HTTPException(status_code=409, detail="job is no longer open")

    # Charge inside the locked region so capacity + payment commit atomically.
    # On PaymentError everything rolls back and the offer stays acceptable; the
    # outbound key means a retry gets the SAME PaymentIntent back — no strays.
    # ponytail: holds a row lock across a network call; fine at template scale,
    # move to a two-phase outbox if provider latency ever hurts.
    try:
        charge = provider.charge_buyer(
            buyer_id=job.buyer_id,
            amount=job.buyer_price,
            currency=settings.currency,
            job_id=str(job.id),
            idempotency_key=f"charge:{job.id}",
        )
    except PaymentError as exc:
        logger.warning("charge failed for job %s: %s", job.id, exc)
        raise HTTPException(status_code=502, detail="payment provider unavailable, retry") from None
    session.add(
        Payment(
            job_id=job.id,
            buyer_id=job.buyer_id,
            amount=job.buyer_price,
            currency=settings.currency,
            status=charge.status,
            provider=provider.name,
            provider_payment_id=charge.provider_payment_id,
            client_secret=charge.client_secret,
            fee_estimate=estimated_fee(job.buyer_price, repo.fee_config(session)),
        )
    )

    offer.status = OfferStatus.ACCEPTED
    offer.responded_at = _now()
    job.seller_id = seller_id
    job.seller_payout = offer.seller_payout
    job.accepted_at = _now()
    job.status = (
        JobStatus.ACCEPTED
        if charge.status is PaymentStatus.SUCCEEDED
        else JobStatus.AWAITING_PAYMENT
    )
    notifications.enqueue(
        session,
        EventKind.JOB_ACCEPTED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "buyer_price": str(job.buyer_price),
            "awaiting_payment": job.status is JobStatus.AWAITING_PAYMENT,
        },
    )
    session.flush()
    return job


@seller_router.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: UUID, session: SessionDep, seller_id: SellerId) -> dict[str, str]:
    offer = session.get(Offer, offer_id, with_for_update=True)
    if offer is None or offer.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="offer not found")
    if offer.status != OfferStatus.OFFERED:
        raise HTTPException(status_code=409, detail=f"offer is {offer.status}, not open")
    offer.status = OfferStatus.DECLINED
    offer.responded_at = _now()
    job = session.get(Job, offer.job_id, with_for_update=True)
    if job is not None and job.status == JobStatus.PENDING:
        _match_and_offer(session, job)  # walk to the next eligible seller
    return {"status": "ok"}


@seller_router.post("/jobs/{job_id}/complete", response_model=SellerCompletionOut)
def complete_job(
    job_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Transaction:
    job = session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.seller_id != seller_id:
        raise HTTPException(status_code=403, detail="job not assigned to this seller")
    if job.status != JobStatus.ACCEPTED:
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not accepted")
    if job.seller_payout is None:
        raise HTTPException(status_code=500, detail="accepted job missing payout")

    job.status = JobStatus.COMPLETED
    job.completed_at = _now()
    tx = Transaction(
        job_id=job.id,
        buyer_price=job.buyer_price,
        seller_payout=job.seller_payout,
        margin=to_money(job.buyer_price - job.seller_payout),
    )
    session.add(tx)

    # Escrow exit: move the payout to the seller. A transfer failure does NOT
    # fail completion — the work happened; the debt is recorded and retried via
    # POST /v1/admin/payouts/{id}/retry.
    seller = repo.get_or_create_seller(session, seller_id)
    payout = Payout(
        job_id=job.id, seller_id=seller_id, amount=job.seller_payout, currency=settings.currency
    )
    if seller.provider_account_id is None:
        payout.status = PayoutStatus.FAILED  # unonboarded (shouldn't match, but never lose money)
    else:
        try:
            transfer = provider.transfer_to_seller(
                provider_account_id=seller.provider_account_id,
                amount=job.seller_payout,
                currency=settings.currency,
                job_id=str(job.id),
                idempotency_key=f"transfer:{job.id}",
            )
            payout.provider_transfer_id = transfer.provider_transfer_id
            payout.status = transfer.status
        except PaymentError as exc:
            # payout isn't flushed yet (no id), so identify it by job + seller.
            logger.warning("transfer failed for job %s (seller %s): %s", job.id, seller_id, exc)
            payout.status = PayoutStatus.FAILED
    session.add(payout)
    session.flush()  # payout.id exists for the admin notification payload
    notifications.enqueue(
        session,
        EventKind.JOB_COMPLETED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "buyer_price": str(job.buyer_price),
        },
    )
    if payout.status is PayoutStatus.FAILED:
        notifications.enqueue_admins(
            session,
            EventKind.PAYOUT_FAILED_ADMIN,
            {
                "job_id": str(job.id),
                "payout_id": str(payout.id),
                "seller_id": seller_id,
                "amount": str(job.seller_payout),
            },
        )

    repo.get_or_create_buyer(session, job.buyer_id).completed_jobs += 1
    seller.completed_jobs += 1
    session.flush()
    return tx


# ---------- Admin router ----------

admin_router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    route_class=CommitRoute,
)


@admin_router.get("/config")
def get_config(session: SessionDep) -> dict[str, Any]:
    pc = repo.get_platform_config(session)
    service_types = {
        st.id: {
            "base_buyer_price": str(st.base_buyer_price),
            "base_seller_payout": str(st.base_seller_payout),
        }
        for st in session.scalars(select(ServiceType)).all()
    }
    pipelines = {
        p.service_type_id: {"buyer": p.buyer, "seller": p.seller}
        for p in session.scalars(select(Pipeline)).all()
    }
    return {
        "service_types": service_types,
        "pipelines": pipelines,
        "margin_floor": {
            "absolute": str(pc.margin_absolute),
            "pct": str(pc.margin_pct),
            "ceiling_multiplier": str(pc.ceiling_multiplier),
        },
        "fees": {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)},
        "matching_strategy": pc.matching_strategy,
        "adjuster_params": pc.adjuster_params,
    }


@admin_router.put("/config/service_types/{service_type_id}", response_model=ServiceTypeOut)
def upsert_service_type(
    service_type_id: str, body: ServiceTypeBody, session: SessionDep, admin_id: AdminId
) -> ServiceType:
    st = session.get(ServiceType, service_type_id)
    if st is None:
        st = ServiceType(id=service_type_id)
        session.add(st)
    st.base_buyer_price = body.base_buyer_price
    st.base_seller_payout = body.base_seller_payout
    audit(session, admin_id, "upsert_service_type", service_type_id, body.model_dump(mode="json"))
    session.flush()
    return st


@admin_router.put("/config/pipelines/{service_type_id}")
def upsert_pipelines(
    service_type_id: str, body: PipelinesBody, session: SessionDep, admin_id: AdminId
) -> dict[str, list[str]]:
    if session.get(ServiceType, service_type_id) is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    unknown = sorted({n for n in (*body.buyer, *body.seller) if n not in REGISTRY})
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown adjuster(s): {unknown}")
    pipe = session.get(Pipeline, service_type_id)
    if pipe is None:
        pipe = Pipeline(service_type_id=service_type_id)
        session.add(pipe)
    pipe.buyer = body.buyer
    pipe.seller = body.seller
    audit(session, admin_id, "upsert_pipelines", service_type_id, body.model_dump(mode="json"))
    return {"buyer": pipe.buyer, "seller": pipe.seller}


@admin_router.put("/config/margin_floor")
def update_margin_floor(
    body: MarginFloorBody, session: SessionDep, admin_id: AdminId
) -> dict[str, str]:
    pc = repo.get_platform_config(session)
    pc.margin_absolute = body.absolute
    pc.margin_pct = body.pct
    pc.ceiling_multiplier = body.ceiling_multiplier
    audit(session, admin_id, "update_margin_floor", "platform", body.model_dump(mode="json"))
    return {
        "absolute": str(pc.margin_absolute),
        "pct": str(pc.margin_pct),
        "ceiling_multiplier": str(pc.ceiling_multiplier),
    }


@admin_router.put("/config/fees")
def update_fees(body: FeesBody, session: SessionDep, admin_id: AdminId) -> dict[str, str]:
    pc = repo.get_platform_config(session)
    pc.fee_pct = body.pct
    pc.fee_fixed = body.fixed
    audit(session, admin_id, "update_fees", "platform", body.model_dump(mode="json"))
    return {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)}


@admin_router.put("/config/matching_strategy")
def update_matching_strategy(
    body: MatchingStrategyBody, session: SessionDep, admin_id: AdminId
) -> dict[str, str]:
    if body.strategy not in STRATEGIES:
        raise HTTPException(
            status_code=422, detail=f"strategy must be one of {sorted(STRATEGIES.keys())}"
        )
    pc = repo.get_platform_config(session)
    pc.matching_strategy = body.strategy
    audit(session, admin_id, "update_matching_strategy", "platform", {"strategy": body.strategy})
    return {"strategy": body.strategy}


@admin_router.put("/config/adjuster_params/{adjuster_name}")
def update_adjuster_params(
    adjuster_name: str, body: dict[str, Any], session: SessionDep, admin_id: AdminId
) -> dict[str, Any]:
    if adjuster_name not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown adjuster {adjuster_name!r}")
    pc = repo.get_platform_config(session)
    pc.adjuster_params = {**pc.adjuster_params, adjuster_name: body}  # reassign so it persists
    audit(session, admin_id, "update_adjuster_params", adjuster_name, body)
    return body


@admin_router.put("/sellers/{seller_id}", response_model=SellerProfileOut)
def admin_update_seller(
    seller_id: str, body: AdminSellerBody, session: SessionDep, admin_id: AdminId
) -> SellerProfile:
    seller = repo.get_or_create_seller(session, seller_id)
    if body.tier is not None:
        seller.tier = body.tier
    if body.capacity is not None:
        seller.capacity = body.capacity
    audit(
        session,
        admin_id,
        "update_seller",
        seller_id,
        body.model_dump(mode="json", exclude_none=True),
    )
    session.flush()
    return seller


@admin_router.post("/users/{user_id}/suspend", response_model=UserModerationOut)
def suspend_user(
    user_id: str, body: SuspendRequest, session: SessionDep, admin_id: AdminId
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.role is UserRole.ADMIN:
        raise HTTPException(status_code=422, detail="admins cannot be suspended")
    if user.status is UserStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="user already suspended")
    user.status = UserStatus.SUSPENDED
    user.suspended_reason = body.reason
    user.suspended_at = _now()
    audit(session, admin_id, "suspend_user", user_id, {"reason": body.reason})
    session.flush()
    return user


@admin_router.post("/users/{user_id}/reinstate", response_model=UserModerationOut)
def reinstate_user(user_id: str, session: SessionDep, admin_id: AdminId) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.status is not UserStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="user is not suspended")
    user.status = UserStatus.ACTIVE
    user.suspended_reason = None
    user.suspended_at = None
    audit(session, admin_id, "reinstate_user", user_id, {})
    session.flush()
    return user


def _admin_review_out(row: Review | SellerReview) -> AdminReviewOut:
    author, subject = (
        (row.buyer_id, row.seller_id) if isinstance(row, Review) else (row.seller_id, row.buyer_id)
    )
    return AdminReviewOut(
        id=row.id,
        job_id=row.job_id,
        author_id=author,
        subject_id=subject,
        rating=row.rating,
        comment=row.comment,
        comment_hidden=row.comment_hidden,
        created_at=row.created_at,
    )


@admin_router.get("/reviews/{kind}", response_model=list[AdminReviewOut])
def admin_list_reviews(
    kind: Literal["buyer", "seller"],
    session: SessionDep,
    admin_id: AdminId,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[AdminReviewOut]:
    model = Review if kind == "buyer" else SellerReview
    rows = session.scalars(select(model).order_by(model.created_at.desc())).all()
    return [_admin_review_out(r) for r in _paginate(rows, limit, offset)]


@admin_router.get("/listings", response_model=list[SellerListingOut])
def admin_list_listings(
    session: SessionDep, limit: Limit = 100, offset: Offset = 0
) -> list[Listing]:
    rows = session.scalars(select(Listing).order_by(Listing.created_at.desc())).all()
    return _paginate(list(rows), limit, offset)


@admin_router.post("/listings/{listing_id}/hide", response_model=SellerListingOut)
def hide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")
    listing.hidden = True
    audit(session, admin_id, "hide_listing", str(listing_id), {})
    session.flush()
    return listing


@admin_router.post("/listings/{listing_id}/unhide", response_model=SellerListingOut)
def unhide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")
    listing.hidden = False
    audit(session, admin_id, "unhide_listing", str(listing_id), {})
    session.flush()
    return listing


def _set_comment_hidden(
    kind: Literal["buyer", "seller"],
    review_id: UUID,
    hidden: bool,
    session: Session,
    admin_id: str,
) -> AdminReviewOut:
    model = Review if kind == "buyer" else SellerReview
    row = session.get(model, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review not found")
    if row.comment_hidden == hidden:
        raise HTTPException(status_code=409, detail="review already in that state")
    row.comment_hidden = hidden
    audit(
        session,
        admin_id,
        "hide_review" if hidden else "unhide_review",
        f"{kind}:{review_id}",
        {},
    )
    session.flush()
    return _admin_review_out(row)


@admin_router.post("/reviews/{kind}/{review_id}/hide", response_model=AdminReviewOut)
def admin_hide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, True, session, admin_id)


@admin_router.post("/reviews/{kind}/{review_id}/unhide", response_model=AdminReviewOut)
def admin_unhide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, False, session, admin_id)


@admin_router.post("/users/{user_id}/reset_display_name", response_model=UserModerationOut)
def admin_reset_display_name(user_id: str, session: SessionDep, admin_id: AdminId) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.display_name = f"user-{user.id[:8]}"
    audit(session, admin_id, "reset_display_name", user_id, {})
    session.flush()
    return user


@admin_router.get("/reports", response_model=list[AdminReportOut])
def admin_list_reports(
    session: SessionDep,
    admin_id: AdminId,
    status: ReportStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Report]:
    q = select(Report).order_by(Report.created_at.desc())
    if status is not None:
        q = q.where(Report.status == status)
    return _paginate(session.scalars(q).all(), limit, offset)


@admin_router.post("/reports/{report_id}/resolve", response_model=AdminReportOut)
def resolve_report(
    report_id: UUID, body: ResolveReportRequest, session: SessionDep, admin_id: AdminId
) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    if report.status is not ReportStatus.OPEN:
        raise HTTPException(status_code=409, detail="report already resolved")
    report.status = body.status
    report.resolution_note = body.note
    report.resolved_at = _now()
    audit(
        session,
        admin_id,
        "resolve_report",
        str(report_id),
        {"status": body.status, "note": body.note or ""},
    )
    session.flush()
    return report


@admin_router.get("/buyers", response_model=list[BuyerProfileOut])
def admin_list_buyers(
    session: SessionDep, admin_id: AdminId, limit: Limit = 100, offset: Offset = 0
) -> list[BuyerProfile]:
    rows = session.scalars(select(BuyerProfile).order_by(BuyerProfile.id)).all()
    return _paginate(rows, limit, offset)


@admin_router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    session: SessionDep, limit: Limit = 100, offset: Offset = 0
) -> list[Transaction]:
    rows = session.scalars(select(Transaction).order_by(Transaction.completed_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.get("/payouts", response_model=list[PayoutOut])
def list_payouts(
    session: SessionDep,
    status: PayoutStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Payout]:
    stmt = select(Payout)
    if status is not None:
        stmt = stmt.where(Payout.status == status)
    rows = session.scalars(stmt.order_by(Payout.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/payouts/{payout_id}/retry", response_model=PayoutOut)
def retry_payout(
    payout_id: UUID, session: SessionDep, admin_id: AdminId, provider: ProviderDep
) -> Payout:
    payout = session.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise HTTPException(status_code=404, detail="payout not found")
    if payout.status is not PayoutStatus.FAILED:
        raise HTTPException(status_code=409, detail=f"payout is {payout.status}, not failed")
    seller = session.get(SellerProfile, payout.seller_id)
    if seller is None or seller.provider_account_id is None:
        raise HTTPException(status_code=409, detail="seller has no payment account yet")
    # If no transfer was ever created (plain outage), replaying the original key
    # is the safe retry — it can never double-pay. But if a transfer WAS created
    # and later reversed (transfer.reversed → FAILED), replaying that key would
    # return the same reversed transfer and record PAID with no money moved, so
    # the retry must force a new transfer under a fresh key.
    retry_key = (
        f"transfer:{payout.job_id}"
        if payout.provider_transfer_id is None
        else f"transfer:{payout.job_id}:retry:{payout.provider_transfer_id}"
    )
    try:
        transfer = provider.transfer_to_seller(
            provider_account_id=seller.provider_account_id,
            amount=payout.amount,
            currency=payout.currency,
            job_id=str(payout.job_id),
            idempotency_key=retry_key,
        )
    except PaymentError as exc:
        logger.warning("transfer retry failed for payout %s: %s", payout_id, exc)
        raise HTTPException(status_code=502, detail="payment provider unavailable, retry") from None
    payout.provider_transfer_id = transfer.provider_transfer_id
    payout.status = transfer.status
    audit(session, admin_id, "retry_payout", str(payout_id), {})
    return payout


@admin_router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    session: SessionDep,
    status: NotificationStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Notification]:
    stmt = select(Notification)
    if status is not None:
        stmt = stmt.where(Notification.status == status)
    rows = session.scalars(stmt.order_by(Notification.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/notifications/drain")
def drain_notifications(session: SessionDep, admin_id: AdminId) -> dict[str, int]:
    """Manual drain for ops/cron — the in-process loop normally handles this."""
    sent = notifications.drain_once(get_mail_sender())
    audit(session, admin_id, "drain_notifications", "notifications", {"sent": sent})
    return {"sent": sent}


@admin_router.get("/disputes", response_model=list[AdminDisputeOut])
def list_disputes(
    session: SessionDep,
    status: DisputeStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Dispute]:
    stmt = select(Dispute)
    if status is not None:
        stmt = stmt.where(Dispute.status == status)
    rows = session.scalars(stmt.order_by(Dispute.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/disputes/{dispute_id}/resolve", response_model=AdminDisputeOut)
def resolve_dispute(
    dispute_id: UUID,
    body: ResolveDisputeRequest,
    session: SessionDep,
    admin_id: AdminId,
    provider: ProviderDep,
) -> Dispute:
    dispute = session.get(Dispute, dispute_id, with_for_update=True)
    if dispute is None:
        raise HTTPException(status_code=404, detail="dispute not found")
    # RESOLVED is the only terminal state. A chargeback_won/lost dispute can
    # still be arbitrated — one dispute per job means that without this, a
    # lost chargeback would permanently block the platform from ever clawing
    # back the at-fault seller (no second dispute can ever exist for the job).
    if dispute.status is DisputeStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="dispute already resolved")
    # Unlocked read: buyer_price/seller_payout are write-once at job
    # completion, so there is nothing concurrent for this read to race.
    job = session.get(Job, dispute.job_id)
    if job is None or job.seller_payout is None:
        raise HTTPException(status_code=500, detail="disputed job missing payout")
    refund_amount = to_money(body.refund_amount)
    clawback_amount = to_money(body.clawback_amount)
    if refund_amount > job.buyer_price:
        raise HTTPException(status_code=422, detail="refund exceeds the buyer price")
    if clawback_amount > job.seller_payout:
        raise HTTPException(status_code=422, detail="clawback exceeds the seller payout")
    # A prior attempt may have already pinned amounts (see below): a provider
    # leg from that attempt may have already executed, so a retry with
    # DIFFERENT amounts can't be allowed to silently diverge from what the
    # provider already did — real Stripe would enforce this per idempotency
    # key anyway, so make it a 409 here instead of a mystery later.
    if (dispute.refund_amount is not None and dispute.refund_amount != refund_amount) or (
        dispute.clawback_amount is not None and dispute.clawback_amount != clawback_amount
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"a prior attempt pinned refund={dispute.refund_amount} "
                f"clawback={dispute.clawback_amount}; retry must reuse them"
            ),
        )

    # Existence guards precede ALL provider legs (hoisted above the try below):
    # a 4xx must never fire after a provider call has already moved money, and
    # unlike a 502 a 409 here doesn't invite the retry that would converge.
    payment: Payment | None = None
    if refund_amount > 0:
        payment = session.scalar(select(Payment).where(Payment.job_id == job.id))
        if payment is None:
            raise HTTPException(status_code=409, detail="no payment recorded for this job")
    payout: Payout | None = None
    if clawback_amount > 0:
        payout = session.scalar(select(Payout).where(Payout.job_id == job.id))
        if (
            payout is None
            or payout.provider_transfer_id is None
            or payout.status is not PayoutStatus.PAID
        ):
            # A FAILED payout (e.g. a fully reversed transfer) has no money
            # the seller actually kept — clawing back against it would book a
            # lying CLAWBACK row (fake provider) or 502 forever (real Stripe).
            raise HTTPException(status_code=409, detail="no paid transfer to claw back")

    # Pin the amounts now that every 4xx guard has passed, and commit: a
    # provider leg below may still fail and 502, but the pin survives that
    # rollback (get_session's except-block only rolls back work since the
    # last commit), so a retry is forced to converge on these amounts instead
    # of silently changing them after a leg may have already executed.
    dispute.refund_amount = refund_amount
    dispute.clawback_amount = clawback_amount
    session.commit()

    # Provider legs, both idempotent by key: a failure raises 502 with nothing
    # further recorded, and the retry replays the succeeded leg (same key)
    # then completes the other — no partial-resolution state exists beyond
    # the pinned amounts above.
    refund_ref: str | None = None
    reversal_ref: str | None = None
    try:
        if refund_amount > 0:
            assert payment is not None  # guaranteed by the guard above
            refund_ref = provider.refund(
                payment.provider_payment_id,
                idempotency_key=f"refund:{job.id}:dispute",
                amount=refund_amount,
            ).provider_refund_id
            # Payment.status deliberately untouched: a partial refund leaves
            # the charge partly intact; REFUNDED stays the cancel path's state.
        if clawback_amount > 0:
            assert (
                payout is not None and payout.provider_transfer_id is not None
            )  # guaranteed above
            reversal_ref = provider.reverse_transfer(
                payout.provider_transfer_id,
                amount=clawback_amount,
                idempotency_key=f"reversal:{job.id}:dispute",
            ).provider_reversal_id
    except PaymentError as exc:
        logger.warning("dispute resolution provider call failed for %s: %s", dispute_id, exc)
        raise HTTPException(status_code=502, detail="payment provider unavailable, retry") from None

    # The pin's commit above released the row lock taken at the top of this
    # function — re-acquire it before mutating status. NOT get_one: it does
    # emit SELECT FOR UPDATE (the lock is taken), but with
    # expire_on_commit=False it hands back the unexpired identity-map object
    # with STALE attributes — a status check against it would miss a
    # concurrent winner. populate_existing refreshes the object from the
    # locked row. A concurrent chargeback_closed that flipped status
    # mid-flight loses to arbitration by design (RESOLVED overwrites it, and
    # the RESOLVED-preservation rule in _apply_payment_event stops any later
    # chargeback event from undoing it).
    dispute = session.scalars(
        select(Dispute)
        .where(Dispute.id == dispute_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if dispute.status is DisputeStatus.RESOLVED:
        # A concurrent duplicate resolve won the race during the lock-free
        # legs window. Both requests replayed the same idempotency keys —
        # money moved once — but booking again here would double the ledger,
        # so the loser stops.
        raise HTTPException(status_code=409, detail="dispute already resolved")
    dispute.status = DisputeStatus.RESOLVED
    dispute.refund_amount = refund_amount
    dispute.clawback_amount = clawback_amount
    dispute.resolution_note = body.note
    dispute.resolved_at = _now()
    if refund_amount > 0:
        session.add(
            Adjustment(
                job_id=job.id,
                dispute_id=dispute.id,
                kind=AdjustmentKind.REFUND,
                amount=refund_amount,
                provider_ref=refund_ref,
            )
        )
    if clawback_amount > 0:
        session.add(
            Adjustment(
                job_id=job.id,
                dispute_id=dispute.id,
                kind=AdjustmentKind.CLAWBACK,
                amount=clawback_amount,
                provider_ref=reversal_ref,
            )
        )
    notifications.enqueue(
        session,
        EventKind.DISPUTE_RESOLVED_BUYER,
        dispute.buyer_id,
        {"job_id": str(job.id), "refund_amount": str(refund_amount)},
    )
    if job.seller_id is not None:
        notifications.enqueue(
            session,
            EventKind.DISPUTE_RESOLVED_SELLER,
            job.seller_id,
            {"job_id": str(job.id), "clawback_amount": str(clawback_amount)},
        )
    audit(
        session,
        admin_id,
        "resolve_dispute",
        str(dispute_id),
        {"refund": str(refund_amount), "clawback": str(clawback_amount)},
    )
    return dispute


@admin_router.get("/margins/summary", response_model=MarginSummaryOut)
def margins_summary(session: SessionDep) -> MarginSummaryOut:
    """Margin summary. Fee fields are a CASH view: a fee is sunk the moment a
    charge captures, so refunded jobs' fees show as losses and charged-but-in-
    flight jobs dip net until their margin books at completion."""
    txs = session.scalars(select(Transaction)).all()
    revenue = sum((t.buyer_price for t in txs), to_money(0))
    payouts = sum((t.seller_payout for t in txs), to_money(0))
    margin = sum((t.margin for t in txs), to_money(0))
    take_rate = float(margin / revenue) if revenue > 0 else 0.0
    adjustments = session.scalars(select(Adjustment)).all()
    signs = {
        AdjustmentKind.REFUND: -1,
        AdjustmentKind.CLAWBACK: 1,
        AdjustmentKind.CHARGEBACK_LOSS: -1,
        AdjustmentKind.CHARGEBACK_FEE: -1,
    }
    adjustments_net = sum((a.amount * signs[a.kind] for a in adjustments), to_money(0))
    fees = sum(
        (
            p_fee
            for p_fee in session.scalars(
                select(Payment.fee_estimate).where(
                    Payment.status.in_((PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED))
                )
            ).all()
        ),
        to_money(0),
    )
    return MarginSummaryOut(
        transactions=len(txs),
        gross_revenue=revenue,
        seller_payouts=payouts,
        platform_margin=margin,
        take_rate=round(take_rate, 4),
        adjustments_net=to_money(adjustments_net),
        platform_margin_net=to_money(margin + adjustments_net),
        fees_estimated=to_money(fees),
        platform_margin_net_of_fees=to_money(margin + adjustments_net - fees),
    )


@admin_router.get("/stats", response_model=StatsOut)
def admin_stats(session: SessionDep, admin_id: AdminId) -> StatsOut:
    """Operator snapshot: counts only, one session, curl-able."""
    oldest_pending = session.scalar(
        select(func.min(Notification.created_at)).where(
            Notification.status == NotificationStatus.PENDING
        )
    )
    users = _count_by(session, User.role, UserRole)
    users["suspended"] = (
        session.scalar(
            select(func.count()).select_from(User).where(User.status == UserStatus.SUSPENDED)
        )
        or 0
    )
    notification_counts = _count_by(session, Notification.status, NotificationStatus)
    return StatsOut(
        jobs=_count_by(session, Job.status, JobStatus),
        payments=_count_by(session, Payment.status, PaymentStatus),
        payouts=_count_by(session, Payout.status, PayoutStatus),
        notifications=NotificationStats(
            pending=notification_counts["pending"],
            sent=notification_counts["sent"],
            failed=notification_counts["failed"],
            oldest_pending_age_seconds=(
                (_now() - oldest_pending).total_seconds() if oldest_pending else None
            ),
        ),
        disputes_open=session.scalar(
            select(func.count()).select_from(Dispute).where(Dispute.status == DisputeStatus.OPEN)
        )
        or 0,
        reports_open=session.scalar(
            select(func.count()).select_from(Report).where(Report.status == ReportStatus.OPEN)
        )
        or 0,
        users=users,
        quotes_live=session.scalar(
            select(func.count()).select_from(Quote).where(Quote.expires_at > _now())
        )
        or 0,
        retention={
            "idempotency_keys": session.scalar(select(func.count()).select_from(IdempotencyRecord))
            or 0,
            "webhook_events": session.scalar(select(func.count()).select_from(WebhookEvent)) or 0,
            "notifications_total": session.scalar(select(func.count()).select_from(Notification))
            or 0,
        },
        uptime_seconds=int((_now() - _STARTED_AT).total_seconds()),
    )


@admin_router.get("/audit", response_model=list[AuditOut])
def list_audit(session: SessionDep, limit: Limit = 100, offset: Offset = 0) -> list[AuditLog]:
    rows = session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.get("/jobs", response_model=list[BuyerJobView])
def list_all_jobs(
    session: SessionDep, status: JobStatus | None = None, limit: Limit = 100, offset: Offset = 0
) -> list[Job]:
    stmt = select(Job)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    rows = session.scalars(stmt.order_by(Job.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def admin_cancel_job(
    job_id: UUID, session: SessionDep, admin_id: AdminId, provider: ProviderDep
) -> Job:
    job = session.get(Job, job_id)  # unlocked: existence only
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Canonical lock order is Payment → Job (see cancel_job) — never Job first.
    session.scalar(select(Payment).where(Payment.job_id == job_id).with_for_update())
    # populate_existing: the unlocked existence get() above cached this Job in
    # the identity map; without it the locked re-get returns the STALE cached
    # attributes (expire_on_commit=False), so a status guard racing a complete
    # reads pre-race state and cancels an already-COMPLETED job (finding F5).
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.EXPIRED):
        raise HTTPException(status_code=409, detail=f"cannot cancel a {job.status} job")
    _expire_open_offers(session, job.id)
    try:
        refunded = _release_payment(session, provider, job)
    except PaymentError as exc:
        logger.warning("payment release failed for job %s: %s", job.id, exc)
        raise HTTPException(status_code=502, detail="payment provider unavailable, retry") from None
    _notify_cancelled(session, job, refunded)
    job.status = JobStatus.CANCELLED
    audit(session, admin_id, "cancel_job", str(job_id), {})
    return job


@admin_router.post("/jobs/sweep")
def sweep(session: SessionDep, admin_id: AdminId, provider: ProviderDep) -> dict[str, str]:
    _sweep(session, provider)
    audit(session, admin_id, "sweep", "jobs", {})
    return {"status": "ok"}


# ---------- Payments router (webhooks) ----------

payments_router = APIRouter(prefix="/v1/payments", tags=["payments"], route_class=CommitRoute)


def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
    """Route a normalized provider event to the row it affects.

    Unknown kinds and unknown ids are recorded (dedup) and ignored — providers
    emit dozens of event types this app doesn't act on."""
    if event.kind in ("payment_succeeded", "payment_failed"):
        payment = session.scalar(
            select(Payment).where(Payment.provider_payment_id == event.object_id).with_for_update()
        )
        if payment is None or payment.status is PaymentStatus.REFUNDED:
            return  # refunded is terminal — late events never resurrect the charge
        if event.kind == "payment_succeeded":
            job = session.get(Job, payment.job_id, with_for_update=True)
            if payment.status is PaymentStatus.FAILED and (
                job is None or job.status != JobStatus.AWAITING_PAYMENT
            ):
                # The charge was voided (cancel/sweep) and the job is dead — a late
                # success must never resurrect it. Real providers don't emit success
                # for a voided PI; races and replays can.
                logger.warning(
                    "ignoring late payment_succeeded for voided payment %s (job %s)",
                    payment.id,
                    payment.job_id,
                )
                return
            payment.status = PaymentStatus.SUCCEEDED
            if job is not None and job.status == JobStatus.AWAITING_PAYMENT:
                job.status = JobStatus.ACCEPTED
        elif payment.status is not PaymentStatus.SUCCEEDED:
            payment.status = PaymentStatus.FAILED  # late failures never undo a success
    elif event.kind == "account_updated":
        seller = session.scalar(
            select(SellerProfile).where(SellerProfile.provider_account_id == event.object_id)
        )
        if seller is not None and event.payments_ready is not None:
            seller.payments_ready = event.payments_ready
    elif event.kind in ("transfer_paid", "transfer_failed"):
        payout = session.scalar(
            select(Payout).where(Payout.provider_transfer_id == event.object_id).with_for_update()
        )
        if payout is not None:
            if event.kind == "transfer_failed":
                payout.status = PayoutStatus.FAILED
                notifications.enqueue_admins(
                    session,
                    EventKind.PAYOUT_FAILED_ADMIN,
                    {
                        "job_id": str(payout.job_id),
                        "payout_id": str(payout.id),
                        "seller_id": payout.seller_id,
                        "amount": str(payout.amount),
                    },
                )
            else:
                payout.status = PayoutStatus.PAID
    elif event.kind == "chargeback_opened":
        if event.related_id is None:
            logger.warning(
                "chargeback event %s has no payment_intent; cannot map to a job", event.event_id
            )
            return
        payment = session.scalar(
            select(Payment).where(Payment.provider_payment_id == event.related_id)
        )
        if payment is None:
            return  # unknown charge: recorded by dedup, nothing to apply
        dispute = session.scalar(
            select(Dispute).where(Dispute.job_id == payment.job_id).with_for_update()
        )
        if dispute is None:
            dispute = Dispute(
                job_id=payment.job_id,
                source=DisputeSource.PROVIDER,
                buyer_id=payment.buyer_id,
                reason="provider chargeback",
                provider_dispute_id=event.object_id,
            )
            session.add(dispute)
            session.flush()
        else:
            dispute.provider_dispute_id = event.object_id  # annotate, don't duplicate
        amount = to_money(Decimal(event.amount_minor or 0) / 100)
        notifications.enqueue_admins(
            session,
            EventKind.CHARGEBACK_OPENED_ADMIN,
            {"job_id": str(payment.job_id), "dispute_id": str(dispute.id), "amount": str(amount)},
        )
    elif event.kind == "chargeback_closed":
        dispute = session.scalar(
            select(Dispute).where(Dispute.provider_dispute_id == event.object_id).with_for_update()
        )
        if dispute is None:
            return
        won = event.outcome == "won"
        if dispute.status is not DisputeStatus.RESOLVED:
            # The status field records arbitration when an admin has ruled
            # (RESOLVED is preserved); otherwise the latest provider outcome
            # wins — repeat chargebacks on one job re-adjudicate the same row.
            dispute.status = DisputeStatus.CHARGEBACK_WON if won else DisputeStatus.CHARGEBACK_LOST
            dispute.resolved_at = _now()
        amount = to_money(Decimal(event.amount_minor or 0) / 100)
        if not won:
            if amount > 0:  # a zero/absent amount_minor books no $0.00 loss row
                session.add(
                    Adjustment(
                        job_id=dispute.job_id,
                        dispute_id=dispute.id,
                        kind=AdjustmentKind.CHARGEBACK_LOSS,
                        amount=amount,
                        provider_ref=event.object_id,
                    )
                )
            session.add(
                Adjustment(
                    job_id=dispute.job_id,
                    dispute_id=dispute.id,
                    kind=AdjustmentKind.CHARGEBACK_FEE,
                    amount=to_money(settings.chargeback_fee_usd),
                )
            )
        notifications.enqueue_admins(
            session,
            EventKind.CHARGEBACK_CLOSED_ADMIN,
            {
                "job_id": str(dispute.job_id),
                "outcome": event.outcome or "unknown",
                "amount": str(amount),
            },
        )


# Backoff: webhook_retry_base_seconds * 5**(attempts-1) -> 1m, 5m, 25m, ...
_WEBHOOK_BACKOFF_FACTOR = 5
_WEBHOOK_RETRY_BATCH = 100  # bounds how long one retry transaction holds its locks


def _payment_event_payload(event: PaymentEvent) -> dict[str, Any]:
    """JSON-safe snapshot for replay: `PaymentEvent` is a flat frozen
    dataclass of scalars, so this round-trips through `PaymentEvent(**p)`.
    The raw body and signature are never captured at all."""
    return dataclasses.asdict(event)


def _error_summary(exc: Exception) -> str:
    """Exception type plus the first line of its message. Driver errors put
    the SQL statement and its bound parameters on later lines; those stay in
    the server log (logger.exception) and out of the row and admin mail."""
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}"[:512]


def _record_webhook_failure(session: Session, row: WebhookEvent, exc: Exception) -> None:
    """Advance a FAILED row's backoff, or dead-letter it at the attempt cap.

    An APPLIED row is left alone: the event succeeded in a concurrent
    delivery and this failure was the race, not the event. A DEAD_LETTER row
    is terminal and admins were already told."""
    if row.status is not WebhookEventStatus.FAILED:
        return
    row.attempts += 1
    row.last_error = _error_summary(exc)
    if row.attempts >= settings.webhook_max_attempts:
        row.status = WebhookEventStatus.DEAD_LETTER
        row.next_attempt_at = None
        notifications.enqueue_admins(
            session,
            EventKind.WEBHOOK_DEAD_LETTER_ADMIN,
            {
                "webhook_id": str(row.id),
                "provider_event_id": row.provider_event_id,
                "kind": row.kind,
                "last_error": row.last_error,
            },
        )
    else:
        row.next_attempt_at = _now() + timedelta(
            seconds=settings.webhook_retry_base_seconds
            * _WEBHOOK_BACKOFF_FACTOR ** (row.attempts - 1)
        )


def _persist_failed_webhook(event: PaymentEvent, exc: Exception) -> None:
    """Record a first-delivery failure in a FRESH session: the delivering
    session is poisoned by the exception, and its insert of this row rolled
    back with everything else.

    A concurrent delivery of the same event may own the row by now: if it
    applied, the row is APPLIED and stays so; if it is mid-insert, our insert
    loses the unique constraint and we re-select its row instead."""
    with SessionLocal() as session:
        row = session.scalar(
            select(WebhookEvent)
            .where(WebhookEvent.provider_event_id == event.event_id)
            .with_for_update()
        )
        if row is None:
            row = WebhookEvent(
                id=uuid4(),  # set now: the dead-letter notice may need it pre-flush
                provider_event_id=event.event_id,
                kind=event.kind,
                object_id=event.object_id,
                status=WebhookEventStatus.FAILED,
                attempts=0,
                payload=_payment_event_payload(event),
            )
            session.add(row)
            _record_webhook_failure(session, row, exc)
            try:
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                # One re-select, not a loop: the row exists once the winner's
                # insert committed, which is what raised this.
                row = session.scalar(
                    select(WebhookEvent)
                    .where(WebhookEvent.provider_event_id == event.event_id)
                    .with_for_update()
                )
                assert row is not None
        _record_webhook_failure(session, row, exc)
        session.commit()


def _webhook_seen(session: Session, event_id: str) -> bool:
    return (
        session.scalar(select(WebhookEvent.id).where(WebhookEvent.provider_event_id == event_id))
        is not None
    )


def _process_webhook(event: PaymentEvent) -> dict[str, str]:
    """Dedup + apply on a worker thread — sync Session stays off the loop.

    Any existing row for the event id, whatever its status, makes this a
    `duplicate`: a FAILED event's recovery belongs to the retry job, never to
    a redelivery racing it. The dedup row is flushed before the apply so two
    concurrent first deliveries meet at the unique constraint: the loser
    waits for the winner and answers `duplicate`, never a 500.

    A handler failure never 500s the provider (which would only invite
    redelivery storms): the row is persisted FAILED with backoff fields and
    the answer is 200 `deferred`."""
    with SessionLocal() as session:
        if _webhook_seen(session, event.event_id):
            return {"status": "duplicate"}
        session.add(
            WebhookEvent(
                provider_event_id=event.event_id,
                kind=event.kind,
                object_id=event.object_id,
                status=WebhookEventStatus.APPLIED,
                attempts=0,
                payload=_payment_event_payload(event),
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return {"status": "duplicate"}
        try:
            _apply_payment_event(session, event)
            session.commit()
        except Exception as exc:
            logger.exception(
                "webhook %s (%s) failed to apply; deferred to the retry job",
                event.event_id,
                event.kind,
            )
            session.rollback()
            _persist_failed_webhook(event, exc)
            return {"status": "deferred"}
    return {"status": "ok"}


def _run_webhook_retry_once() -> None:
    """One outbox retry pass on a worker thread (the maintenance loop's sweep
    cadence). Claims due FAILED rows with SKIP LOCKED so two app instances
    never retry the same row, and re-applies each stored event through the
    same `_apply_payment_event` under its own savepoint, so one row's failure
    rolls back only that row's effects."""
    with SessionLocal() as session:
        rows = session.scalars(
            select(WebhookEvent)
            .where(
                WebhookEvent.status == WebhookEventStatus.FAILED,
                WebhookEvent.next_attempt_at <= _now(),
            )
            .order_by(WebhookEvent.next_attempt_at)
            .limit(_WEBHOOK_RETRY_BATCH)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                with session.begin_nested():
                    if row.payload is None:
                        raise ValueError("no stored payload to replay")
                    _apply_payment_event(session, PaymentEvent(**row.payload))
            except Exception as exc:
                logger.exception(
                    "webhook retry failed for %s (%s)", row.provider_event_id, row.kind
                )
                _record_webhook_failure(session, row, exc)
                continue
            row.status = WebhookEventStatus.APPLIED
            row.next_attempt_at = None
        session.commit()


@admin_router.get("/webhooks", response_model=list[WebhookEventOut])
def list_webhooks(
    session: SessionDep,
    status: WebhookEventStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[WebhookEvent]:
    stmt = select(WebhookEvent)
    if status is not None:
        stmt = stmt.where(WebhookEvent.status == status)
    rows = session.scalars(stmt.order_by(WebhookEvent.received_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/webhooks/{webhook_id}/retry", response_model=WebhookEventOut)
def retry_webhook(webhook_id: UUID, session: SessionDep, admin_id: AdminId) -> WebhookEvent:
    """Re-queue a FAILED or DEAD_LETTER event for the retry job: attempts
    reset, due now. The job applies it on its next tick; this endpoint never
    applies it inline, so there is one apply path."""
    row = session.get(WebhookEvent, webhook_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="webhook event not found")
    if row.status is WebhookEventStatus.APPLIED:
        raise HTTPException(status_code=409, detail="webhook event already applied")
    if row.payload is None:
        raise HTTPException(status_code=409, detail="webhook event has no stored payload")
    row.status = WebhookEventStatus.FAILED
    row.attempts = 0
    row.next_attempt_at = _now()
    audit(session, admin_id, "retry_webhook", str(webhook_id), {})
    session.flush()
    return row


@payments_router.post("/webhook")
async def payments_webhook(request: Request, provider: ProviderDep) -> dict[str, str]:
    """Provider event sink. Unauthenticated by design — authenticity comes from
    the provider's signature, verified in parse_webhook. Duplicates no-op.

    The fake provider verifies nothing, so without Stripe the route does not
    exist (404) unless `settings.allow_unsigned_webhooks` is on."""
    if provider.name == "fake" and not settings.allow_unsigned_webhooks:
        raise HTTPException(status_code=404, detail="Not Found")
    payload = await request.body()
    try:
        event = provider.parse_webhook(payload, request.headers.get("stripe-signature"))
    except WebhookSignatureError:
        raise HTTPException(status_code=400, detail="invalid webhook signature") from None
    except (PaymentError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed webhook payload") from None
    return await asyncio.to_thread(_process_webhook, event)


# ---------- Catalog router (unauthenticated public browse) ----------

catalog_router = APIRouter(prefix="/v1/catalog", tags=["catalog"], route_class=CommitRoute)

# _listing_bookable, _visible_listings_stmt, _catalog_seller_out,
# _catalog_listing_out, _catalog_listings_for_rows: moved to catalog.py
# (module docstring above, near buyer_router) — aliased there.


@catalog_router.get("/listings", response_model=list[CatalogListingOut])
def catalog_list_listings(
    session: SessionDep,
    service_type_id: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[CatalogListingOut]:
    stmt = _visible_listings_stmt().order_by(Listing.created_at.desc())
    if service_type_id is not None:
        stmt = stmt.where(Listing.service_type_id == service_type_id)
    rows = _paginate(session.execute(stmt).all(), limit, offset)
    return _catalog_listings_for_rows(session, rows)


@catalog_router.get("/listings/{listing_id}", response_model=CatalogListingOut)
def catalog_get_listing(listing_id: UUID, session: SessionDep) -> CatalogListingOut:
    row = session.execute(_visible_listings_stmt().where(Listing.id == listing_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="listing not found")
    listing, user, seller = row
    price = _indicative_buyer_price(session, listing.service_type_id)
    if price is None:
        raise HTTPException(status_code=404, detail="listing not found")
    return _catalog_listing_out(listing, user, seller, price)


@catalog_router.get("/sellers/{seller_id}", response_model=CatalogSellerProfileOut)
def catalog_get_seller(seller_id: str, session: SessionDep) -> CatalogSellerProfileOut:
    user = session.get(User, seller_id)
    seller = session.get(SellerProfile, seller_id)
    if not catalog.seller_publicly_visible(user, seller):
        raise HTTPException(status_code=404, detail="seller not found")
    assert user is not None
    assert seller is not None
    rows = session.execute(_visible_listings_stmt().where(Listing.seller_id == seller_id)).all()
    return CatalogSellerProfileOut(
        seller_id=user.id,
        display_name=user.display_name,
        rating=seller.rating,
        rating_count=seller.rating_count,
        completed_jobs=seller.completed_jobs,
        listings=_catalog_listings_for_rows(session, rows),
    )


# ---------- App assembly ----------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Re-assert at startup too: the module-level check fails open for any
    # router registered after it (e.g. a conditional include below it).
    _assert_commit_routes(app)
    # Create tables for SQLite/dev. Production applies Alembic migrations instead.
    if settings.database_url.startswith("sqlite"):
        init_db()
    bootstrap_admin()
    logger.info("marketplace starting (db=%s)", settings.database_url.split("://", 1)[0])
    maintenance = asyncio.create_task(_maintenance_loop())
    yield
    maintenance.cancel()
    with suppress(asyncio.CancelledError):
        await maintenance


def _add_hardening_middleware(app: FastAPI) -> None:
    """Settings-driven: CORS only when origins are configured; TrustedHost
    defaults open (*) until narrowed; body cap always on."""
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(BodySizeLimitMiddleware)


configure_logging()
app = FastAPI(title="Marketplace", version="1.0.0", lifespan=_lifespan)
app.add_middleware(IdempotencyMiddleware)
_add_hardening_middleware(app)
app.add_middleware(RequestIdMiddleware)  # outermost, unchanged


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(buyer_router)
app.include_router(seller_router)
app.include_router(admin_router)
app.include_router(reports_router)
app.include_router(prefs_router)
app.include_router(payments_router)
app.include_router(catalog_router)
app.include_router(pages_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


_COMMIT_EXEMPT = {"/healthz"}


def _assert_commit_routes(app: FastAPI) -> None:
    """Every route in the app must commit-before-response (finding F2); a
    plain APIRoute would silently regress to commit-in-teardown. Widened
    (pages foundation) from a /v1-only check to app-wide: the pages layer
    (pages.py) mutates no state today, but a future page/form route missing
    route_class=CommitRoute must fail the same way an API route would.
    Mounts (e.g. the /static StaticFiles app) aren't APIRoute instances and
    are skipped; /healthz is the sole deliberate exemption."""
    rogue = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and not isinstance(route, CommitRoute)
        and route.path not in _COMMIT_EXEMPT
    ]
    if rogue:
        raise RuntimeError(f"routes missing route_class=CommitRoute (finding F2): {rogue}")


_assert_commit_routes(app)
