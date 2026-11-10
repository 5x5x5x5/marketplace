"""Operator routes (`/v1/admin/...`): the only routes that see both sides'
numbers. Every route requires an admin token (router-level dependency)."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import require_admin
from ..db import CommitRoute
from ..entities import (
    AuditLog,
    BuyerProfile,
    Dispute,
    Job,
    Listing,
    Notification,
    Payout,
    Report,
    SellerProfile,
    ServiceType,
    Transaction,
    User,
    WebhookEvent,
)
from ..models import (
    AdminDisputeOut,
    AdminReportOut,
    AdminReviewOut,
    AdminSellerBody,
    AuditOut,
    BuyerJobView,
    BuyerProfileOut,
    DisputeStatus,
    FeesBody,
    JobStatus,
    MarginFloorBody,
    MarginSummaryOut,
    MatchingStrategyBody,
    NotificationOut,
    NotificationStatus,
    PayoutOut,
    PayoutStatus,
    PipelinesBody,
    ReportStatus,
    ResolveDisputeRequest,
    ResolveReportRequest,
    SellerListingOut,
    SellerProfileOut,
    ServiceTypeBody,
    ServiceTypeOut,
    StatsOut,
    SuspendRequest,
    TransactionOut,
    UserModerationOut,
    WebhookEventOut,
    WebhookEventStatus,
)
from ..services import (
    disputes,
    jobs,
    listings,
    maintenance,
    moderation,
    ops,
    payouts,
    platform_config,
    profiles,
    reviews,
    webhooks,
)
from .deps import AdminId, Limit, Offset, ProviderDep, SessionDep

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
