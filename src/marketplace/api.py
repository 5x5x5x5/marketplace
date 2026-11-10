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
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from . import catalog
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
    AuditLog,
    BuyerProfile,
    Dispute,
    Job,
    Listing,
    Notification,
    Offer,
    Payout,
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
from .models import (
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
    NotificationStatus,
    OfferStatus,
    OnboardingOut,
    PayoutOut,
    PayoutStatus,
    PipelinesBody,
    QuoteOut,
    QuoteRequest,
    ReportOut,
    ReportRequest,
    ReportStatus,
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
    WebhookEventOut,
    WebhookEventStatus,
)
from .observability import BodySizeLimitMiddleware, RequestIdMiddleware, configure_logging
from .pages import pages_router
from .payments import get_provider
from .payments.port import PaymentError, PaymentProvider, WebhookSignatureError
from .services import (
    booking,
    disputes,
    jobs,
    listings,
    maintenance,
    moderation,
    ops,
    payouts,
    platform_config,
    preferences,
    profiles,
    reviews,
    webhooks,
)
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


# ---------- Matching / offer helpers ----------


# ---------- Reports router ----------

reports_router = APIRouter(prefix="/v1", tags=["reports"], route_class=CommitRoute)
ParticipantClaims = Annotated[Claims, Depends(current_participant)]


@reports_router.post("/reports", response_model=ReportOut, status_code=201)
def file_report(body: ReportRequest, session: SessionDep, claims: ParticipantClaims) -> Report:
    return moderation.file_report(session, claims.sub, body)


@reports_router.get("/reports", response_model=list[ReportOut])
def my_reports(session: SessionDep, claims: ParticipantClaims) -> list[Report]:
    return moderation.my_reports(session, claims.sub)


# ---------- Notification preferences router ----------

prefs_router = APIRouter(prefix="/v1", tags=["preferences"], route_class=CommitRoute)


@prefs_router.get("/notification-preferences", response_model=list[NotificationPreferenceOut])
def get_notification_preferences(
    session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return preferences.get_notification_preferences(session, claims.sub, claims.role)


@prefs_router.put("/notification-preferences", response_model=list[NotificationPreferenceOut])
def put_notification_preferences(
    body: NotificationPreferencesUpdate, session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return preferences.put_notification_preferences(session, claims.sub, claims.role, body)


# ---------- Buyer router ----------


buyer_router = APIRouter(prefix="/v1", tags=["buyer"], route_class=CommitRoute)


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
    return profiles.get_buyer_profile(session, buyer_id)


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
    return profiles.update_profile(session, seller_id, body)


@seller_router.get("/profile", response_model=SellerProfileOut)
def get_profile(session: SessionDep, seller_id: SellerId) -> SellerProfile:
    return profiles.get_profile(session, seller_id)


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
    profiles.post_availability(session, seller_id, req)
    return {"status": "ok"}


@seller_router.delete("/availability/{service_type_id}")
def delete_availability(
    service_type_id: str, session: SessionDep, seller_id: SellerId
) -> dict[str, str]:
    profiles.delete_availability(session, seller_id, service_type_id)
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
    return listings.create_listing(session, seller_id, body)


@seller_router.get("/listings", response_model=list[SellerListingOut])
def my_listings(session: SessionDep, seller_id: SellerId) -> list[Listing]:
    return listings.my_listings(session, seller_id)


@seller_router.put("/listings/{listing_id}", response_model=SellerListingOut)
def update_listing(
    listing_id: UUID, body: ListingUpdate, session: SessionDep, seller_id: SellerId
) -> Listing:
    return listings.update_listing(session, seller_id, listing_id, body)


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
    return platform_config.get_config(session)


@admin_router.put("/config/service_types/{service_type_id}", response_model=ServiceTypeOut)
def upsert_service_type(
    service_type_id: str, body: ServiceTypeBody, session: SessionDep, admin_id: AdminId
) -> ServiceType:
    return platform_config.upsert_service_type(session, admin_id, service_type_id, body)


@admin_router.put("/config/pipelines/{service_type_id}")
def upsert_pipelines(
    service_type_id: str, body: PipelinesBody, session: SessionDep, admin_id: AdminId
) -> dict[str, list[str]]:
    return platform_config.upsert_pipelines(session, admin_id, service_type_id, body)


@admin_router.put("/config/margin_floor")
def update_margin_floor(
    body: MarginFloorBody, session: SessionDep, admin_id: AdminId
) -> dict[str, str]:
    return platform_config.update_margin_floor(session, admin_id, body)


@admin_router.put("/config/fees")
def update_fees(body: FeesBody, session: SessionDep, admin_id: AdminId) -> dict[str, str]:
    return platform_config.update_fees(session, admin_id, body)


@admin_router.put("/config/matching_strategy")
def update_matching_strategy(
    body: MatchingStrategyBody, session: SessionDep, admin_id: AdminId
) -> dict[str, str]:
    return platform_config.update_matching_strategy(session, admin_id, body)


@admin_router.put("/config/adjuster_params/{adjuster_name}")
def update_adjuster_params(
    adjuster_name: str, body: dict[str, Any], session: SessionDep, admin_id: AdminId
) -> dict[str, Any]:
    return platform_config.update_adjuster_params(session, admin_id, adjuster_name, body)


@admin_router.put("/sellers/{seller_id}", response_model=SellerProfileOut)
def admin_update_seller(
    seller_id: str, body: AdminSellerBody, session: SessionDep, admin_id: AdminId
) -> SellerProfile:
    return profiles.admin_update_seller(session, admin_id, seller_id, body)


@admin_router.post("/users/{user_id}/suspend", response_model=UserModerationOut)
def suspend_user(
    user_id: str, body: SuspendRequest, session: SessionDep, admin_id: AdminId
) -> User:
    return moderation.suspend_user(session, admin_id, user_id, body)


@admin_router.post("/users/{user_id}/reinstate", response_model=UserModerationOut)
def reinstate_user(user_id: str, session: SessionDep, admin_id: AdminId) -> User:
    return moderation.reinstate_user(session, admin_id, user_id)


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
    return listings.admin_list_listings(session, limit, offset)


@admin_router.post("/listings/{listing_id}/hide", response_model=SellerListingOut)
def hide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    return listings.hide_listing(session, admin_id, listing_id)


@admin_router.post("/listings/{listing_id}/unhide", response_model=SellerListingOut)
def unhide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    return listings.unhide_listing(session, admin_id, listing_id)


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
    return moderation.admin_reset_display_name(session, admin_id, user_id)


@admin_router.get("/reports", response_model=list[AdminReportOut])
def admin_list_reports(
    session: SessionDep,
    admin_id: AdminId,
    status: ReportStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Report]:
    return moderation.admin_list_reports(session, status, limit, offset)


@admin_router.post("/reports/{report_id}/resolve", response_model=AdminReportOut)
def resolve_report(
    report_id: UUID, body: ResolveReportRequest, session: SessionDep, admin_id: AdminId
) -> Report:
    return moderation.resolve_report(session, admin_id, report_id, body)


@admin_router.get("/buyers", response_model=list[BuyerProfileOut])
def admin_list_buyers(
    session: SessionDep, admin_id: AdminId, limit: Limit = 100, offset: Offset = 0
) -> list[BuyerProfile]:
    return profiles.admin_list_buyers(session, limit, offset)


@admin_router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    session: SessionDep, limit: Limit = 100, offset: Offset = 0
) -> list[Transaction]:
    return ops.list_transactions(session, limit, offset)


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
    return ops.list_notifications(session, status, limit, offset)


@admin_router.post("/notifications/drain")
def drain_notifications(session: SessionDep, admin_id: AdminId) -> dict[str, int]:
    """Manual drain for ops/cron — the in-process loop normally handles this."""
    return ops.drain_notifications(session, admin_id)


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
    return ops.margins_summary(session)


@admin_router.get("/stats", response_model=StatsOut)
def admin_stats(session: SessionDep, admin_id: AdminId) -> StatsOut:
    """Operator snapshot: counts only, one session, curl-able."""
    return ops.admin_stats(session)


@admin_router.get("/audit", response_model=list[AuditOut])
def list_audit(session: SessionDep, limit: Limit = 100, offset: Offset = 0) -> list[AuditLog]:
    return ops.list_audit(session, limit, offset)


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


@catalog_router.get("/listings", response_model=list[CatalogListingOut])
def catalog_list_listings(
    session: SessionDep,
    service_type_id: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[CatalogListingOut]:
    return catalog.list_listings(session, service_type_id, limit, offset)


@catalog_router.get("/listings/{listing_id}", response_model=CatalogListingOut)
def catalog_get_listing(listing_id: UUID, session: SessionDep) -> CatalogListingOut:
    return catalog.get_listing(session, listing_id)


@catalog_router.get("/sellers/{seller_id}", response_model=CatalogSellerProfileOut)
def catalog_get_seller(seller_id: str, session: SessionDep) -> CatalogSellerProfileOut:
    return catalog.get_seller_profile(session, seller_id)


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
