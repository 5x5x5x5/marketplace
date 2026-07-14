"""Transactional-outbox notifications.

Domain transitions call `enqueue` INSIDE their transaction, so rolled-back
events never mail and committed ones eventually always do. `drain_once` —
called by the maintenance loop in api.py and the admin drain endpoint —
claims due rows and sends them through the mail port with retry/backoff.

Payloads are role-safe snapshots built at enqueue time: seller payloads never
contain buyer_price, buyer payloads never contain seller_payout. Import
direction is one-way: api -> notifications -> (mail, db, entities, models).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .entities import Notification, User
from .mail import EmailSender
from .models import EventKind, NotificationStatus, UserRole
from .settings import settings

logger = logging.getLogger("marketplace.notifications")

_BACKOFF_BASE_SECONDS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(session: Session, kind: EventKind, user_id: str, payload: dict[str, Any]) -> None:
    """Queue a notification in the caller's transaction. A missing recipient is
    a logged skip — a stale id must never break a money transaction."""
    user = session.get(User, user_id)
    if user is None:
        logger.warning("notification %s skipped: no user %s", kind, user_id)
        return
    session.add(Notification(user_id=user.id, email=user.email, kind=kind, payload=payload))


def enqueue_admins(session: Session, kind: EventKind, payload: dict[str, Any]) -> None:
    """Fan a notification out to every admin account; none -> logged skip."""
    admins = session.scalars(select(User).where(User.role == UserRole.ADMIN)).all()
    if not admins:
        logger.warning("notification %s skipped: no admin accounts", kind)
        return
    for admin in admins:
        session.add(Notification(user_id=admin.id, email=admin.email, kind=kind, payload=payload))


# ---------- Renderers: pure payload -> (subject, body) ----------


def _render_offer_received(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"New offer: {p['service_type_id']} paying {p['seller_payout']}",
        (
            f"You have a new offer for {p['service_type_id']} paying {p['seller_payout']}.\n"
            f"It expires at {p['expires_at']} - accept it in the app before then.\n"
            f"Job: {p['job_id']}"
        ),
    )


def _render_job_accepted_buyer(p: dict[str, Any]) -> tuple[str, str]:
    lines = [
        f"A seller accepted your {p['service_type_id']} job ({p['buyer_price']}).",
        f"Job: {p['job_id']}",
    ]
    if p.get("awaiting_payment"):
        lines.insert(1, "Complete your payment in the app to start the work.")
    return (f"Your {p['service_type_id']} job was accepted", "\n".join(lines))


def _render_job_completed_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Your {p['service_type_id']} job is complete",
        (
            f"Your {p['service_type_id']} job ({p['buyer_price']}) is complete.\n"
            f"You can leave a review in the app.\nJob: {p['job_id']}"
        ),
    )


def _render_job_expired_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Your {p['service_type_id']} job expired",
        (
            f"Your {p['service_type_id']} job expired: {p['reason']}.\n"
            f"You can request a new quote any time.\nJob: {p['job_id']}"
        ),
    )


def _render_job_cancelled_seller(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Job cancelled: {p['service_type_id']}",
        (
            f"The {p['service_type_id']} job you accepted ({p['seller_payout']}) was cancelled.\n"
            f"Your slot is free again.\nJob: {p['job_id']}"
        ),
    )


def _render_refund_issued_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Your payment was refunded",
        f"Your payment of {p['buyer_price']} was refunded in full.\nJob: {p['job_id']}",
    )


def _render_payout_failed_admin(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Payout FAILED for job {p['job_id']}",
        (
            f"Transfer of {p['amount']} to seller {p['seller_id']} failed.\n"
            f"Retry via POST /v1/admin/payouts/{p['payout_id']}/retry.\nJob: {p['job_id']}"
        ),
    )


def _render_dispute_opened_seller(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Dispute opened on your {p['service_type_id']} job",
        (
            f"The buyer disputed your {p['service_type_id']} job.\n"
            f'Reason: "{p["reason"]}"\n'
            f"An operator will review it; you may be contacted.\nJob: {p['job_id']}"
        ),
    )


def _render_dispute_opened_admin(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Arbitration needed: dispute on job {p['job_id']}",
        (
            f'Reason: "{p["reason"]}"\n'
            f"Resolve via POST /v1/admin/disputes/{p['dispute_id']}/resolve.\n"
            f"Job: {p['job_id']}"
        ),
    )


def _render_dispute_resolved_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Your dispute was resolved",
        (
            f"Your dispute on job {p['job_id']} was resolved.\n"
            f"Refund issued to you: {p['refund_amount']}."
        ),
    )


def _render_dispute_resolved_seller(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "A dispute on your job was resolved",
        (
            f"The dispute on job {p['job_id']} was resolved.\n"
            f"Amount reclaimed from your payout: {p['clawback_amount']}."
        ),
    )


RENDERERS: dict[EventKind, Callable[[dict[str, Any]], tuple[str, str]]] = {
    EventKind.OFFER_RECEIVED: _render_offer_received,
    EventKind.JOB_ACCEPTED_BUYER: _render_job_accepted_buyer,
    EventKind.JOB_COMPLETED_BUYER: _render_job_completed_buyer,
    EventKind.JOB_EXPIRED_BUYER: _render_job_expired_buyer,
    EventKind.JOB_CANCELLED_SELLER: _render_job_cancelled_seller,
    EventKind.REFUND_ISSUED_BUYER: _render_refund_issued_buyer,
    EventKind.PAYOUT_FAILED_ADMIN: _render_payout_failed_admin,
    EventKind.DISPUTE_OPENED_SELLER: _render_dispute_opened_seller,
    EventKind.DISPUTE_OPENED_ADMIN: _render_dispute_opened_admin,
    EventKind.DISPUTE_RESOLVED_BUYER: _render_dispute_resolved_buyer,
    EventKind.DISPUTE_RESOLVED_SELLER: _render_dispute_resolved_seller,
}


def drain_once(mail: EmailSender, limit: int = 20) -> int:
    """Send due pending rows; returns the number sent.

    Safe to run concurrently: rows are claimed FOR UPDATE SKIP LOCKED (real on
    Postgres, serialized single-writer on SQLite). Per-row try/except - one bad
    row backs off without blocking the queue."""
    sent = 0
    with SessionLocal() as session:
        rows = session.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.PENDING,
                Notification.next_attempt_at <= _now(),
            )
            .order_by(Notification.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                subject, body = RENDERERS[row.kind](row.payload)
                mail.send(row.email, subject, body)
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)[:512]
                if row.attempts >= settings.notify_max_attempts:
                    row.status = NotificationStatus.FAILED
                else:
                    row.next_attempt_at = _now() + timedelta(
                        seconds=_BACKOFF_BASE_SECONDS * 2**row.attempts
                    )
                continue
            row.status = NotificationStatus.SENT
            row.sent_at = _now()
            sent += 1
        session.commit()
    return sent
