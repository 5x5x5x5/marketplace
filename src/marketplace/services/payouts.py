"""Seller payment accounts and payouts: onboarding, the payout list, and the
admin retry of a failed transfer (with its fresh-key rule for reversals)."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import repo
from ..entities import Payout, SellerProfile
from ..errors import ConflictError, NotFoundError, ProviderUnavailableError
from ..models import OnboardingOut, PayoutStatus
from ..payments.port import PaymentError, PaymentProvider
from ..repo import audit
from ..settings import settings
from .common import paginate as _paginate
from .common import require_active as _require_active

logger = logging.getLogger("marketplace")


def onboard_payments(session: Session, seller_id: str, provider: PaymentProvider) -> OnboardingOut:
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
            raise ProviderUnavailableError("payment provider unavailable, retry") from None
        seller.provider_account_id = acct.provider_account_id
        seller.payments_ready = acct.payments_ready
        session.flush()
    return OnboardingOut(
        onboarding_url=provider.onboarding_link(
            seller.provider_account_id, settings.onboarding_return_url
        ),
        payments_ready=seller.payments_ready,
    )


def list_payouts(
    session: Session, status: PayoutStatus | None, limit: int, offset: int
) -> list[Payout]:
    stmt = select(Payout)
    if status is not None:
        stmt = stmt.where(Payout.status == status)
    return _paginate(
        session, stmt.order_by(Payout.created_at.desc(), Payout.id.desc()), limit, offset
    )


def retry_payout(
    session: Session, admin_id: str, payout_id: UUID, provider: PaymentProvider
) -> Payout:
    payout = session.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise NotFoundError("payout not found")
    if payout.status is not PayoutStatus.FAILED:
        raise ConflictError(f"payout is {payout.status}, not failed")
    seller = session.get(SellerProfile, payout.seller_id)
    if seller is None or seller.provider_account_id is None:
        raise ConflictError("seller has no payment account yet")
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
        raise ProviderUnavailableError("payment provider unavailable, retry") from None
    payout.provider_transfer_id = transfer.provider_transfer_id
    payout.status = transfer.status
    audit(session, admin_id, "retry_payout", str(payout_id), {})
    return payout
