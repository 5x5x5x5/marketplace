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
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

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
from .db import CommitRoute, get_session, init_db
from .entities import (
    Adjustment,
    AuditLog,
    Availability,
    BuyerProfile,
    Dispute,
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
from .errors import DomainError
from .idempotency import IdempotencyMiddleware
from .mail import get_mail_sender
from .matching import STRATEGIES
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
from .payments.port import PaymentError, PaymentProvider, WebhookSignatureError
from .pricing import REGISTRY
from .repo import audit
from .services import booking, disputes, jobs, maintenance, payouts, reviews, webhooks
from .services.common import now as _now
from .services.common import paginate as _paginate
from .services.common import require_active as _require_active
from .services.webhooks import process_webhook as _process_webhook
from .settings import settings

logger = logging.getLogger("marketplace")

MAX_PAGE = 200

SessionDep = Annotated[Session, Depends(get_session)]
BuyerId = Annotated[str, Depends(current_buyer)]
SellerId = Annotated[str, Depends(current_seller)]
AdminId = Annotated[str, Depends(require_admin)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0)]
ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]


_STARTED_AT = _now()


# ---------- Matching / offer helpers ----------


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
    return booking.create_quote(session, buyer_id, req)


@buyer_router.post("/jobs", response_model=BuyerJobView, status_code=201)
def create_job(req: JobCreateRequest, session: SessionDep, buyer_id: BuyerId) -> Job:
    return booking.create_job(session, buyer_id, req)


@buyer_router.get("/jobs", response_model=list[BuyerJobView])
def list_buyer_jobs(
    session: SessionDep,
    buyer_id: BuyerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    return jobs.list_buyer_jobs(session, buyer_id, status, limit, offset)


@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep
) -> BuyerJobView:
    return jobs.get_job_buyer(session, buyer_id, job_id, provider)


@buyer_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def cancel_job(job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep) -> Job:
    return jobs.cancel_job(session, buyer_id, job_id, provider)


@buyer_router.get("/profile", response_model=BuyerProfileOut)
def get_buyer_profile(session: SessionDep, buyer_id: BuyerId) -> BuyerProfile:
    return repo.get_or_create_buyer(session, buyer_id)


@buyer_router.post("/jobs/{job_id}/review", response_model=ReviewOut, status_code=201)
def review_job(job_id: UUID, body: ReviewRequest, session: SessionDep, buyer_id: BuyerId) -> Review:
    return reviews.review_job(session, buyer_id, job_id, body)


@buyer_router.post("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut, status_code=201)
def open_dispute(
    job_id: UUID, body: DisputeRequest, session: SessionDep, buyer_id: BuyerId
) -> Dispute:
    return disputes.open_dispute(session, buyer_id, job_id, body)


@buyer_router.get("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut)
def get_dispute_buyer(job_id: UUID, session: SessionDep, buyer_id: BuyerId) -> Dispute:
    return disputes.get_dispute_buyer(session, buyer_id, job_id)


@buyer_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId
) -> list[JobReviewOut]:
    return reviews.list_job_reviews_buyer(session, buyer_id, job_id)


# ---------- Seller router ----------

seller_router = APIRouter(prefix="/v1/seller", tags=["seller"], route_class=CommitRoute)


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
    return payouts.onboard_payments(session, seller_id, provider)


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
    return jobs.list_offers(session, seller_id, provider, status, limit, offset)


@seller_router.get("/jobs", response_model=list[SellerJobView])
def list_seller_jobs(
    session: SessionDep,
    seller_id: SellerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    return jobs.list_seller_jobs(session, seller_id, status, limit, offset)


@seller_router.get("/jobs/{job_id}/dispute", response_model=SellerDisputeOut)
def get_dispute_seller(job_id: UUID, session: SessionDep, seller_id: SellerId) -> Dispute:
    return disputes.get_dispute_seller(session, seller_id, job_id)


@seller_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_seller(
    job_id: UUID, session: SessionDep, seller_id: SellerId
) -> list[JobReviewOut]:
    return reviews.list_job_reviews_seller(session, seller_id, job_id)


@seller_router.post("/jobs/{job_id}/review", response_model=SellerReviewOut, status_code=201)
def review_buyer(
    job_id: UUID, body: ReviewRequest, session: SessionDep, seller_id: SellerId
) -> SellerReview:
    return reviews.review_buyer(session, seller_id, job_id, body)


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
    return jobs.accept_offer(session, seller_id, offer_id, provider)


@seller_router.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: UUID, session: SessionDep, seller_id: SellerId) -> dict[str, str]:
    jobs.decline_offer(session, seller_id, offer_id)
    return {"status": "ok"}


@seller_router.post("/jobs/{job_id}/complete", response_model=SellerCompletionOut)
def complete_job(
    job_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Transaction:
    return jobs.complete_job(session, seller_id, job_id, provider)


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


@admin_router.get("/reviews/{kind}", response_model=list[AdminReviewOut])
def admin_list_reviews(
    kind: Literal["buyer", "seller"],
    session: SessionDep,
    admin_id: AdminId,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[AdminReviewOut]:
    return reviews.admin_list_reviews(session, kind, limit, offset)


@admin_router.get("/listings", response_model=list[SellerListingOut])
def admin_list_listings(
    session: SessionDep, limit: Limit = 100, offset: Offset = 0
) -> list[Listing]:
    stmt = select(Listing).order_by(Listing.created_at.desc(), Listing.id.desc())
    return _paginate(session, stmt, limit, offset)


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


@admin_router.post("/reviews/{kind}/{review_id}/hide", response_model=AdminReviewOut)
def admin_hide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return reviews.admin_hide_review(session, admin_id, kind, review_id)


@admin_router.post("/reviews/{kind}/{review_id}/unhide", response_model=AdminReviewOut)
def admin_unhide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return reviews.admin_unhide_review(session, admin_id, kind, review_id)


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
    q = select(Report).order_by(Report.created_at.desc(), Report.id.desc())
    if status is not None:
        q = q.where(Report.status == status)
    return _paginate(session, q, limit, offset)


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
    return _paginate(session, select(BuyerProfile).order_by(BuyerProfile.id), limit, offset)


@admin_router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    session: SessionDep, limit: Limit = 100, offset: Offset = 0
) -> list[Transaction]:
    stmt = select(Transaction).order_by(Transaction.completed_at.desc(), Transaction.id.desc())
    return _paginate(session, stmt, limit, offset)


@admin_router.get("/payouts", response_model=list[PayoutOut])
def list_payouts(
    session: SessionDep,
    status: PayoutStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Payout]:
    return payouts.list_payouts(session, status, limit, offset)


@admin_router.post("/payouts/{payout_id}/retry", response_model=PayoutOut)
def retry_payout(
    payout_id: UUID, session: SessionDep, admin_id: AdminId, provider: ProviderDep
) -> Payout:
    return payouts.retry_payout(session, admin_id, payout_id, provider)


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
    return _paginate(
        session,
        stmt.order_by(Notification.created_at.desc(), Notification.id.desc()),
        limit,
        offset,
    )


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
    return disputes.list_disputes(session, status, limit, offset)


@admin_router.post("/disputes/{dispute_id}/resolve", response_model=AdminDisputeOut)
def resolve_dispute(
    dispute_id: UUID,
    body: ResolveDisputeRequest,
    session: SessionDep,
    admin_id: AdminId,
    provider: ProviderDep,
) -> Dispute:
    return disputes.resolve_dispute(session, admin_id, dispute_id, body, provider)


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
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    return _paginate(session, stmt, limit, offset)


@admin_router.get("/jobs", response_model=list[BuyerJobView])
def list_all_jobs(
    session: SessionDep, status: JobStatus | None = None, limit: Limit = 100, offset: Offset = 0
) -> list[Job]:
    return jobs.list_all_jobs(session, status, limit, offset)


@admin_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def admin_cancel_job(
    job_id: UUID, session: SessionDep, admin_id: AdminId, provider: ProviderDep
) -> Job:
    return jobs.admin_cancel_job(session, admin_id, job_id, provider)


@admin_router.post("/jobs/sweep")
def sweep(session: SessionDep, admin_id: AdminId, provider: ProviderDep) -> dict[str, str]:
    maintenance.admin_sweep(session, admin_id, provider)
    return {"status": "ok"}


# ---------- Payments router (webhooks) ----------

payments_router = APIRouter(prefix="/v1/payments", tags=["payments"], route_class=CommitRoute)


@admin_router.get("/webhooks", response_model=list[WebhookEventOut])
def list_webhooks(
    session: SessionDep,
    status: WebhookEventStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[WebhookEvent]:
    return webhooks.list_webhooks(session, status, limit, offset)


@admin_router.post("/webhooks/{webhook_id}/retry", response_model=WebhookEventOut)
def retry_webhook(webhook_id: UUID, session: SessionDep, admin_id: AdminId) -> WebhookEvent:
    """Re-queue a FAILED or DEAD_LETTER event for the retry job: attempts
    reset, due now. The job applies it on its next tick; this endpoint never
    applies it inline, so there is one apply path."""
    return webhooks.retry_webhook(session, admin_id, webhook_id)


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
    rows = session.execute(catalog.visible_listings_page(limit, offset, service_type_id)).all()
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
    if not settings.maintenance_enabled:
        logger.info("maintenance loop disabled (MAINTENANCE_ENABLED=false) on this instance")
        yield
        return
    loop_task = asyncio.create_task(maintenance.maintenance_loop())
    yield
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task


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


async def _domain_error_response(request: Request, exc: Exception) -> Response:
    """The one place a service-layer error becomes HTTP: rendered by FastAPI's
    own HTTPException handler, so status, `detail` body and headers are exactly
    what the route would have produced by raising HTTPException itself."""
    assert isinstance(exc, DomainError)  # registered for DomainError only
    return await http_exception_handler(
        request, HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers)
    )


app.add_exception_handler(DomainError, _domain_error_response)


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
