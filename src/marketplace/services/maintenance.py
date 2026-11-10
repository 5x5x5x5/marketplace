"""Sweeps, housekeeping and the maintenance loop.

`sweep` is the lazy-on-read correctness pass (offer expiry with re-match,
stale-payment release) that read endpoints call; `housekeeping` deletes rows
nothing can observe any more and runs only from the loop and the admin sweep.
The loop runs every job on a worker thread so sync session work never blocks
the event loop."""

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import notifications
from ..db import SessionLocal
from ..entities import (
    AuthSession,
    EmailToken,
    IdempotencyRecord,
    Job,
    Notification,
    Payment,
    Quote,
    WebhookEvent,
)
from ..mail import get_mail_sender
from ..models import EventKind, JobStatus, NotificationStatus, PaymentStatus, WebhookEventStatus
from ..payments import get_provider
from ..payments.port import PaymentError, PaymentProvider
from ..repo import audit
from ..settings import settings
from .common import now as _now
from .offers import sweep_expired_offers as _sweep_expired_offers
from .webhooks import run_webhook_retry_once as _run_webhook_retry_once

logger = logging.getLogger("marketplace")


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


def _sweep_expired_quotes(session: Session) -> None:
    """Quotes past their TTL can never be consumed (`create_job` 410s them)."""
    session.execute(delete(Quote).where(Quote.expires_at < _now()))


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


def sweep(session: Session, provider: PaymentProvider) -> None:
    """Lazy-on-read state transitions: expired offers re-match and stale
    payments release their capacity slot, so state is correct even with no
    scheduler running. Housekeeping deletes are NOT here — see below."""
    _sweep_expired_offers(session)
    _sweep_stale_payments(session, provider)


def housekeeping(session: Session) -> None:
    """Deletes of rows nothing can observe any more (expired quotes, sessions,
    email tokens, aged retention rows). Correctness never depends on them, so
    they run only from the maintenance loop and the admin sweep endpoint,
    never on a request's read path."""
    _sweep_expired_quotes(session)
    _sweep_expired_auth(session)
    _sweep_retention(session)


def _run_drain_once() -> None:
    """One outbox drain pass on a worker thread (sync Session stays off the loop)."""
    notifications.drain_once(get_mail_sender())


def _run_sweep_once() -> None:
    with SessionLocal() as session:
        sweep(session, get_provider())
        housekeeping(session)
        session.commit()


async def maintenance_loop() -> None:
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


def admin_sweep(session: Session, admin_id: str, provider: PaymentProvider) -> None:
    """`POST /v1/admin/jobs/sweep`: both passes on demand, audited."""
    sweep(session, provider)
    housekeeping(session)
    audit(session, admin_id, "sweep", "jobs", {})
