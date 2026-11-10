"""Provider webhooks: dedup, apply, and the persisted-failure outbox.

`process_webhook` is the first delivery (run on a worker thread by the
payments router); `run_webhook_retry_once` is the maintenance loop's retry
pass; both funnel through `_apply_payment_event`, whose every branch is safe
to apply twice and safe to apply late. All of it is synchronous session work
and must stay off the event loop."""

import dataclasses
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import notifications
from ..db import SessionLocal
from ..entities import Adjustment, Dispute, Job, Payment, Payout, SellerProfile, WebhookEvent
from ..errors import ConflictError, NotFoundError
from ..models import (
    AdjustmentKind,
    DisputeSource,
    DisputeStatus,
    EventKind,
    JobStatus,
    PaymentStatus,
    PayoutStatus,
    WebhookEventStatus,
    to_money,
)
from ..payments.port import PaymentEvent
from ..repo import audit
from ..settings import settings
from .common import now as _now
from .common import paginate as _paginate

logger = logging.getLogger("marketplace")


def _applied_elsewhere(session: Session, event: PaymentEvent) -> bool:
    """Was an event of this kind for this same provider object already applied
    under a different event id? Only meaningful while that ledger row is
    inside the retention window, so money-moving branches pair it with a
    permanent marker of their own."""
    return (
        session.scalar(
            select(WebhookEvent.id)
            .where(
                WebhookEvent.kind == event.kind,
                WebhookEvent.object_id == event.object_id,
                WebhookEvent.provider_event_id != event.event_id,
                WebhookEvent.status == WebhookEventStatus.APPLIED,
            )
            .limit(1)
        )
        is not None
    )


def _superseded(session: Session, event: PaymentEvent) -> bool:
    """Has a NEWER event of this kind for this object (by arrival) already
    applied? A retried event can land long after later events for the same
    object; for last-value-wins state it must not overwrite them."""
    received = session.scalar(
        select(WebhookEvent.received_at).where(WebhookEvent.provider_event_id == event.event_id)
    )
    if received is None:
        return False
    return (
        session.scalar(
            select(WebhookEvent.id)
            .where(
                WebhookEvent.kind == event.kind,
                WebhookEvent.object_id == event.object_id,
                WebhookEvent.provider_event_id != event.event_id,
                WebhookEvent.status == WebhookEventStatus.APPLIED,
                WebhookEvent.received_at > received,
            )
            .limit(1)
        )
        is not None
    )


def _record_chargeback(
    session: Session, payment: Payment, existing: Dispute | None, event: PaymentEvent
) -> Dispute:
    """One dispute per job: create the provider-sourced row, or annotate the
    existing (buyer-opened, or earlier-chargeback) one."""
    if existing is not None:
        existing.provider_dispute_id = event.object_id
        return existing
    dispute = Dispute(
        job_id=payment.job_id,
        source=DisputeSource.PROVIDER,
        buyer_id=payment.buyer_id,
        reason="provider chargeback",
        provider_dispute_id=event.object_id,
    )
    session.add(dispute)
    session.flush()
    return dispute


def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
    """Route a normalized provider event to the row it affects.

    Unknown kinds and unknown ids are recorded (dedup) and ignored — providers
    emit dozens of event types this app doesn't act on.

    The outbox retry job re-runs this for events whose first apply failed, so
    every branch must be safe to apply twice (the same economic event under
    another event id, or a replay) and safe to apply late (after the job or
    payment moved on, and after later events for the same object applied).
    Each branch says what makes it so."""
    if event.kind in ("payment_succeeded", "payment_failed"):
        # Replay/late safety: the payment status lattice. PENDING -> FAILED ->
        # SUCCEEDED is allowed (decline then retry on the same PI); SUCCEEDED
        # is never undone by a failure; REFUNDED is terminal; a success after
        # a void never resurrects a dead job.
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
        # Last value wins, by arrival: a retried older update must not
        # overwrite a newer one that already applied.
        if _superseded(session, event):
            return
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
                # A failed/reversed transfer is terminal for that transfer id,
                # so applying it late is truthful; notify only on the
                # transition so a replay doesn't re-page admins.
                # A dispute clawback of the whole payout fully reverses the
                # transfer, which the provider reports exactly like a failure.
                # The seller was paid and then clawed back: FAILED would invite
                # an admin payout retry that pays them again. The clawback pin
                # is committed before the reversal leg runs, so it is always
                # visible here, and it is a permanent row (replay-safe).
                clawback = session.scalar(
                    select(Dispute.clawback_amount).where(Dispute.job_id == payout.job_id)
                )
                if clawback is not None and clawback >= payout.amount:
                    logger.info(
                        "transfer %s fully reversed by dispute clawback; payout %s stays %s",
                        event.object_id,
                        payout.id,
                        payout.status,
                    )
                elif payout.status is not PayoutStatus.FAILED:
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
            elif payout.status is PayoutStatus.PENDING:
                payout.status = PayoutStatus.PAID
            elif payout.status is PayoutStatus.FAILED:
                # Late success for a transfer that has since failed/reversed:
                # the money came back, so PAID would be a lie.
                logger.warning(
                    "ignoring late transfer_paid for failed payout %s (transfer %s)",
                    payout.id,
                    event.object_id,
                )
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
        existing = session.scalar(
            select(Dispute).where(Dispute.job_id == payment.job_id).with_for_update()
        )
        if existing is not None and (
            existing.provider_dispute_id == event.object_id or _applied_elsewhere(session, event)
        ):
            # Already recorded (by an earlier apply, or by a chargeback_closed
            # that arrived first): re-annotating or re-notifying is the replay.
            return
        dispute = _record_chargeback(session, payment, existing, event)
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
            # The close can be applied before its open (the open is still
            # waiting on a retry, or never arrived): map it through the
            # disputed payment the same way the open would, rather than
            # dropping the loss on the floor.
            payment = (
                session.scalar(
                    select(Payment).where(Payment.provider_payment_id == event.related_id)
                )
                if event.related_id is not None
                else None
            )
            if payment is None:
                return
            existing = session.scalar(
                select(Dispute).where(Dispute.job_id == payment.job_id).with_for_update()
            )
            dispute = _record_chargeback(session, payment, existing, event)
        won = event.outcome == "won"
        amount = to_money(Decimal(event.amount_minor or 0) / 100)
        # Replay marker: a lost close always books a CHARGEBACK_FEE row stamped
        # with this dispute object's id (permanent, survives retention); a won
        # close moves no money, so the webhook ledger is its marker.
        already_booked = (
            not won
            and session.scalar(
                select(Adjustment.id).where(
                    Adjustment.dispute_id == dispute.id,
                    Adjustment.kind == AdjustmentKind.CHARGEBACK_FEE,
                    Adjustment.provider_ref == event.object_id,
                )
            )
            is not None
        )
        if already_booked or _applied_elsewhere(session, event):
            return
        if dispute.status is not DisputeStatus.RESOLVED:
            # The status field records arbitration when an admin has ruled
            # (RESOLVED is preserved); otherwise the latest provider outcome
            # wins — repeat chargebacks on one job re-adjudicate the same row.
            dispute.status = DisputeStatus.CHARGEBACK_WON if won else DisputeStatus.CHARGEBACK_LOST
            dispute.resolved_at = _now()
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
                    provider_ref=event.object_id,
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


def process_webhook(event: PaymentEvent) -> dict[str, str]:
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


def run_webhook_retry_once() -> None:
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


def list_webhooks(
    session: Session, status: WebhookEventStatus | None, limit: int, offset: int
) -> list[WebhookEvent]:
    stmt = select(WebhookEvent)
    if status is not None:
        stmt = stmt.where(WebhookEvent.status == status)
    stmt = stmt.order_by(WebhookEvent.received_at.desc(), WebhookEvent.id.desc())
    return _paginate(session, stmt, limit, offset)


def retry_webhook(session: Session, admin_id: str, webhook_id: UUID) -> WebhookEvent:
    """Re-queue a FAILED or DEAD_LETTER event for the retry job: attempts
    reset, due now. The job applies it on its next tick; this endpoint never
    applies it inline, so there is one apply path."""
    row = session.get(WebhookEvent, webhook_id, with_for_update=True)
    if row is None:
        raise NotFoundError("webhook event not found")
    if row.status is WebhookEventStatus.APPLIED:
        raise ConflictError("webhook event already applied")
    if row.payload is None:
        raise ConflictError("webhook event has no stored payload")
    row.status = WebhookEventStatus.FAILED
    row.attempts = 0
    row.next_attempt_at = _now()
    audit(session, admin_id, "retry_webhook", str(webhook_id), {})
    session.flush()
    return row
