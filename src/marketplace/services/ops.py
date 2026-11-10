"""Operator read models and actions: the margin summary, the stats snapshot,
the transaction, audit and notification listings, and the manual outbox
drain."""

from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import notifications
from ..entities import (
    Adjustment,
    AuditLog,
    Dispute,
    IdempotencyRecord,
    Job,
    Notification,
    Payment,
    Payout,
    Quote,
    Report,
    Transaction,
    User,
    WebhookEvent,
)
from ..mail import get_mail_sender
from ..models import (
    AdjustmentKind,
    DisputeStatus,
    JobStatus,
    MarginSummaryOut,
    NotificationStats,
    NotificationStatus,
    PaymentStatus,
    PayoutStatus,
    ReportStatus,
    StatsOut,
    UserRole,
    UserStatus,
    to_money,
)
from ..repo import audit
from .common import now as _now
from .common import paginate as _paginate

_STARTED_AT = _now()


def _count_by(session: Session, column: Any, enum: type[StrEnum]) -> dict[str, int]:
    """Group-count a status column; every enum member present, absent = 0."""
    rows: dict[Any, int] = {}
    for value, count in session.execute(select(column, func.count()).group_by(column)).all():
        rows[value] = count
    return {m.value: int(rows.get(m, 0)) for m in enum}


def list_transactions(session: Session, limit: int, offset: int) -> list[Transaction]:
    stmt = select(Transaction).order_by(Transaction.completed_at.desc(), Transaction.id.desc())
    return _paginate(session, stmt, limit, offset)


def list_notifications(
    session: Session, status: NotificationStatus | None, limit: int, offset: int
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


def drain_notifications(session: Session, admin_id: str) -> dict[str, int]:
    """Manual drain for ops/cron — the in-process loop normally handles this."""
    sent = notifications.drain_once(get_mail_sender())
    audit(session, admin_id, "drain_notifications", "notifications", {"sent": sent})
    return {"sent": sent}


def margins_summary(session: Session) -> MarginSummaryOut:
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


def admin_stats(session: Session) -> StatsOut:
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


def list_audit(session: Session, limit: int, offset: int) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    return _paginate(session, stmt, limit, offset)
