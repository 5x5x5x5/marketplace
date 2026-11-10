"""The job lifecycle after booking: listing, accept (which charges the buyer),
decline, complete (which books the margin and pays the seller), and cancel.

Lock order is the whole point of several functions here: Offer ->
SellerProfile -> Job on accept/decline, and Payment -> Job on every cancel
(the same order the webhook and the stale-payment sweep use). Where an
unlocked read precedes the locked one, the locked re-read uses
`populate_existing` so status guards see the row as it is under the lock."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import notifications, repo
from ..entities import Job, Offer, Payment, Payout, SellerProfile, Transaction
from ..errors import (
    ConflictError,
    ForbiddenError,
    GoneError,
    InternalError,
    NotFoundError,
    ProviderUnavailableError,
)
from ..matching import estimated_fee
from ..models import (
    BuyerJobView,
    EventKind,
    JobStatus,
    OfferStatus,
    PaymentStatus,
    PayoutStatus,
    to_money,
)
from ..payments.port import PaymentError, PaymentProvider
from ..repo import audit
from ..settings import settings
from .common import now as _now
from .common import paginate as _paginate
from .common import require_active as _require_active
from .maintenance import sweep as _sweep
from .offers import expire_open_offers as _expire_open_offers
from .offers import match_and_offer as _match_and_offer

logger = logging.getLogger("marketplace")


def _buyer_view(session: Session, job: Job) -> BuyerJobView:
    """BuyerJobView plus the buyer's payment state (never the seller's numbers)."""
    view = BuyerJobView.model_validate(job)
    payment = session.scalar(select(Payment).where(Payment.job_id == job.id))
    if payment is not None:
        view.payment_status = payment.status
        if job.status == JobStatus.AWAITING_PAYMENT:
            view.client_secret = payment.client_secret
    return view


def list_buyer_jobs(
    session: Session, buyer_id: str, status: JobStatus | None, limit: int, offset: int
) -> list[Job]:
    stmt = select(Job).where(Job.buyer_id == buyer_id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return _paginate(session, stmt.order_by(Job.created_at.desc(), Job.id.desc()), limit, offset)


def get_job_buyer(
    session: Session, buyer_id: str, job_id: UUID, provider: PaymentProvider
) -> BuyerJobView:
    _sweep(session, provider)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
    return _buyer_view(session, job)


def cancel_job(session: Session, buyer_id: str, job_id: UUID, provider: PaymentProvider) -> Job:
    job = session.get(Job, job_id)  # unlocked: existence + ownership only
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
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
        raise NotFoundError("job not found")
    if job.status not in (JobStatus.PENDING, JobStatus.AWAITING_PAYMENT):
        raise ConflictError(f"cannot cancel a {job.status} job")
    _expire_open_offers(session, job.id)
    try:
        refunded = _release_payment(session, provider, job)
    except PaymentError as exc:
        logger.warning("payment release failed for job %s: %s", job.id, exc)
        raise ProviderUnavailableError("payment provider unavailable, retry") from None
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


def list_offers(
    session: Session,
    seller_id: str,
    provider: PaymentProvider,
    status: OfferStatus | None,
    limit: int,
    offset: int,
) -> list[Offer]:
    _sweep(session, provider)
    stmt = select(Offer).where(Offer.seller_id == seller_id)
    if status is not None:
        stmt = stmt.where(Offer.status == status)
    else:
        stmt = stmt.where(Offer.status == OfferStatus.OFFERED)
    return _paginate(
        session, stmt.order_by(Offer.offered_at.desc(), Offer.id.desc()), limit, offset
    )


def list_seller_jobs(
    session: Session, seller_id: str, status: JobStatus | None, limit: int, offset: int
) -> list[Job]:
    stmt = select(Job).where(Job.seller_id == seller_id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return _paginate(session, stmt.order_by(Job.created_at.desc(), Job.id.desc()), limit, offset)


def accept_offer(
    session: Session, seller_id: str, offer_id: UUID, provider: PaymentProvider
) -> Job:
    _require_active(session, seller_id)
    offer = session.get(Offer, offer_id, with_for_update=True)
    if offer is None or offer.seller_id != seller_id:
        raise NotFoundError("offer not found")
    if offer.status != OfferStatus.OFFERED:
        raise ConflictError(f"offer is {offer.status}, not open")
    if offer.expires_at < _now():
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()
        raise GoneError("offer expired")

    # Lock the seller row so two concurrent accepts can't exceed capacity.
    seller = session.get(SellerProfile, seller_id, with_for_update=True)
    if seller is None:
        seller = repo.get_or_create_seller(session, seller_id)
    if repo.active_job_count(session, seller_id) >= seller.capacity:
        raise ConflictError("at capacity — complete a job first")

    job = session.get(Job, offer.job_id, with_for_update=True)
    if job is None or job.status != JobStatus.PENDING:
        raise ConflictError("job is no longer open")

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
        raise ProviderUnavailableError("payment provider unavailable, retry") from None
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


def decline_offer(session: Session, seller_id: str, offer_id: UUID) -> None:
    offer = session.get(Offer, offer_id, with_for_update=True)
    if offer is None or offer.seller_id != seller_id:
        raise NotFoundError("offer not found")
    if offer.status != OfferStatus.OFFERED:
        raise ConflictError(f"offer is {offer.status}, not open")
    offer.status = OfferStatus.DECLINED
    offer.responded_at = _now()
    job = session.get(Job, offer.job_id, with_for_update=True)
    if job is not None and job.status == JobStatus.PENDING:
        _match_and_offer(session, job)  # walk to the next eligible seller


def complete_job(
    session: Session, seller_id: str, job_id: UUID, provider: PaymentProvider
) -> Transaction:
    job = session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise NotFoundError("job not found")
    if job.seller_id != seller_id:
        raise ForbiddenError("job not assigned to this seller")
    if job.status != JobStatus.ACCEPTED:
        raise ConflictError(f"job is {job.status}, not accepted")
    if job.seller_payout is None:
        raise InternalError("accepted job missing payout")

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


def list_all_jobs(session: Session, status: JobStatus | None, limit: int, offset: int) -> list[Job]:
    stmt = select(Job)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return _paginate(session, stmt.order_by(Job.created_at.desc(), Job.id.desc()), limit, offset)


def admin_cancel_job(
    session: Session, admin_id: str, job_id: UUID, provider: PaymentProvider
) -> Job:
    job = session.get(Job, job_id)  # unlocked: existence only
    if job is None:
        raise NotFoundError("job not found")
    # Canonical lock order is Payment → Job (see cancel_job) — never Job first.
    session.scalar(select(Payment).where(Payment.job_id == job_id).with_for_update())
    # populate_existing: the unlocked existence get() above cached this Job in
    # the identity map; without it the locked re-get returns the STALE cached
    # attributes (expire_on_commit=False), so a status guard racing a complete
    # reads pre-race state and cancels an already-COMPLETED job (finding F5).
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise NotFoundError("job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.EXPIRED):
        raise ConflictError(f"cannot cancel a {job.status} job")
    _expire_open_offers(session, job.id)
    try:
        refunded = _release_payment(session, provider, job)
    except PaymentError as exc:
        logger.warning("payment release failed for job %s: %s", job.id, exc)
        raise ProviderUnavailableError("payment provider unavailable, retry") from None
    _notify_cancelled(session, job, refunded)
    job.status = JobStatus.CANCELLED
    audit(session, admin_id, "cancel_job", str(job_id), {})
    return job
