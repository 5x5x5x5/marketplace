"""Disputes: buyer-opened disputes, role-scoped reads, and admin resolution.

`resolve_dispute` is one of the two places that commit mid-call: it pins the
refund/clawback amounts and commits before calling the provider, so a 502
from a provider leg still leaves the pin durable for a retry to converge on.
Chargebacks arrive by webhook and are recorded in `services.webhooks`."""

import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import notifications
from ..entities import Adjustment, Dispute, Job, Payment, Payout
from ..errors import (
    ConflictError,
    InternalError,
    NotFoundError,
    ProviderUnavailableError,
    UnprocessableError,
)
from ..models import (
    AdjustmentKind,
    DisputeRequest,
    DisputeSource,
    DisputeStatus,
    EventKind,
    JobStatus,
    PayoutStatus,
    ResolveDisputeRequest,
    to_money,
)
from ..payments.port import PaymentError, PaymentProvider
from ..repo import audit
from ..settings import settings
from .common import now as _now
from .common import paginate as _paginate
from .common import require_active as _require_active

logger = logging.getLogger("marketplace")


def open_dispute(session: Session, buyer_id: str, job_id: UUID, body: DisputeRequest) -> Dispute:
    _require_active(session, buyer_id)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
    if job.status != JobStatus.COMPLETED or job.completed_at is None:
        raise ConflictError("only completed jobs can be disputed")
    if _now() > job.completed_at + timedelta(days=settings.dispute_window_days):
        raise ConflictError("dispute window has elapsed")
    if session.scalar(select(Dispute).where(Dispute.job_id == job_id)) is not None:
        raise ConflictError("job already disputed")

    dispute = Dispute(
        job_id=job.id, source=DisputeSource.BUYER, buyer_id=buyer_id, reason=body.reason
    )
    session.add(dispute)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise ConflictError("job already disputed") from None
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


def get_dispute_buyer(session: Session, buyer_id: str, job_id: UUID) -> Dispute:
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
    dispute = session.scalar(select(Dispute).where(Dispute.job_id == job_id))
    if dispute is None:
        raise NotFoundError("no dispute for this job")
    return dispute


def get_dispute_seller(session: Session, seller_id: str, job_id: UUID) -> Dispute:
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise NotFoundError("job not found")
    dispute = session.scalar(select(Dispute).where(Dispute.job_id == job_id))
    if dispute is None:
        raise NotFoundError("no dispute for this job")
    return dispute


def list_disputes(
    session: Session, status: DisputeStatus | None, limit: int, offset: int
) -> list[Dispute]:
    stmt = select(Dispute)
    if status is not None:
        stmt = stmt.where(Dispute.status == status)
    return _paginate(
        session, stmt.order_by(Dispute.created_at.desc(), Dispute.id.desc()), limit, offset
    )


def resolve_dispute(
    session: Session,
    admin_id: str,
    dispute_id: UUID,
    body: ResolveDisputeRequest,
    provider: PaymentProvider,
) -> Dispute:
    dispute = session.get(Dispute, dispute_id, with_for_update=True)
    if dispute is None:
        raise NotFoundError("dispute not found")
    # RESOLVED is the only terminal state. A chargeback_won/lost dispute can
    # still be arbitrated — one dispute per job means that without this, a
    # lost chargeback would permanently block the platform from ever clawing
    # back the at-fault seller (no second dispute can ever exist for the job).
    if dispute.status is DisputeStatus.RESOLVED:
        raise ConflictError("dispute already resolved")
    # Unlocked read: buyer_price/seller_payout are write-once at job
    # completion, so there is nothing concurrent for this read to race.
    job = session.get(Job, dispute.job_id)
    if job is None or job.seller_payout is None:
        raise InternalError("disputed job missing payout")
    refund_amount = to_money(body.refund_amount)
    clawback_amount = to_money(body.clawback_amount)
    if refund_amount > job.buyer_price:
        raise UnprocessableError("refund exceeds the buyer price")
    if clawback_amount > job.seller_payout:
        raise UnprocessableError("clawback exceeds the seller payout")
    # A prior attempt may have already pinned amounts (see below): a provider
    # leg from that attempt may have already executed, so a retry with
    # DIFFERENT amounts can't be allowed to silently diverge from what the
    # provider already did — real Stripe would enforce this per idempotency
    # key anyway, so make it a 409 here instead of a mystery later.
    if (dispute.refund_amount is not None and dispute.refund_amount != refund_amount) or (
        dispute.clawback_amount is not None and dispute.clawback_amount != clawback_amount
    ):
        raise ConflictError(
            (
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
            raise ConflictError("no payment recorded for this job")
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
            raise ConflictError("no paid transfer to claw back")

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
        raise ProviderUnavailableError("payment provider unavailable, retry") from None

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
        raise ConflictError("dispute already resolved")
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
