"""Payment provider port — the seam between the marketplace and money movers.

The app only ever talks to this protocol; `fake.py` (dev/tests) and
`stripe_provider.py` (production) implement it. Amounts cross the boundary as
2-dp Decimals and are converted to integer minor units at the provider edge.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..models import PaymentStatus, PayoutStatus


class PaymentError(Exception):
    """Provider call failed (network/provider rejection). Callers may retry."""


class WebhookSignatureError(PaymentError):
    """Webhook payload failed signature verification."""


def to_minor_units(amount: Decimal) -> int:
    """2-dp Decimal → integer minor units (cents). Providers speak integers.

    Expects an already-quantized amount (everything passing through
    `models.to_money` is), so the shift is exact.
    """
    return int(amount.scaleb(2).to_integral_value())


@dataclass(frozen=True)
class AccountResult:
    provider_account_id: str
    payments_ready: bool


@dataclass(frozen=True)
class ChargeResult:
    provider_payment_id: str
    status: PaymentStatus
    client_secret: str | None  # buyer-side confirmation secret while status is PENDING


@dataclass(frozen=True)
class TransferResult:
    provider_transfer_id: str
    status: PayoutStatus


@dataclass(frozen=True)
class ReversalResult:
    provider_reversal_id: str


@dataclass(frozen=True)
class RefundResult:
    provider_refund_id: str


@dataclass(frozen=True)
class PaymentEvent:
    """Normalized webhook event. `kind` is one of: payment_succeeded,
    payment_failed, account_updated, transfer_paid, transfer_failed, ignored."""

    event_id: str
    kind: str
    object_id: str  # provider payment/account/transfer id the event refers to
    payments_ready: bool | None = None  # only for account_updated
    amount_minor: int | None = None  # chargeback amount, provider minor units
    outcome: str | None = None  # chargeback_closed: "won" | "lost"
    related_id: str | None = None  # the payment (PI/charge) a chargeback refers to


class PaymentProvider(Protocol):
    name: str

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult: ...

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str: ...

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult: ...

    def cancel_charge(self, provider_payment_id: str) -> None: ...

    def refund(
        self, provider_payment_id: str, *, idempotency_key: str, amount: Decimal | None = None
    ) -> RefundResult:
        """amount=None refunds in full; otherwise refunds the given amount."""
        ...

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult: ...

    def reverse_transfer(
        self, provider_transfer_id: str, *, amount: Decimal, idempotency_key: str
    ) -> ReversalResult: ...

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent: ...
