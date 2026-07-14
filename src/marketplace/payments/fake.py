"""Deterministic in-memory provider for dev and tests.

Charges succeed instantly by default so demos and tests flow without webhooks.
Tests script the async path via one-shot attributes (`next_charge_status`,
`next_transfer_status`) or force an outage with `fail_next_call`. Webhooks are
unsigned JSON — this provider is never selected when STRIPE_SECRET_KEY is set,
so unsigned input is dev-only by construction.
"""

import json
from decimal import Decimal
from itertools import count
from typing import Any

from ..models import PaymentStatus, PayoutStatus
from .port import (
    AccountResult,
    ChargeResult,
    PaymentError,
    PaymentEvent,
    RefundResult,
    ReversalResult,
    TransferResult,
)


class FakeProvider:
    """Recording seams — two deliberate semantics:
    * ATTEMPT-recorded: `transfer_keys` appends before the failure checks, so
      tests can prove WHICH idempotency key a failed attempt used (the
      payout-retry tests rely on this to show original-key replay).
    * SUCCESS-recorded: `refunded`, `refund_keys`, `refund_amounts`,
      `reversals`, `cancelled` append only after the failure checks, so
      counts equal executed provider legs (the dispute-orphan tests rely on
      this). Do not "align" one to the other; both directions are load-bearing.
    """

    name = "fake"

    def __init__(self) -> None:
        self._seq = count(1)
        self.next_charge_status: PaymentStatus = PaymentStatus.SUCCEEDED
        self.next_transfer_status: PayoutStatus = PayoutStatus.PAID
        self.fail_next_call: bool = False
        self.fail_keys: set[str] = set()  # one-shot failures targeted by idempotency key
        self.cancelled: list[str] = []
        self.refunded: list[str] = []
        self.refund_keys: list[str] = []
        self.refund_amounts: list[str | None] = []
        self.transfer_keys: list[str] = []
        self.reversals: list[tuple[str, str, str]] = []

    def reset(self) -> None:
        self.next_charge_status = PaymentStatus.SUCCEEDED
        self.next_transfer_status = PayoutStatus.PAID
        self.fail_next_call = False
        self.fail_keys.clear()
        self.cancelled.clear()
        self.refunded.clear()
        self.refund_keys.clear()
        self.refund_amounts.clear()
        self.transfer_keys.clear()
        self.reversals.clear()

    def _maybe_fail(self) -> None:
        if self.fail_next_call:
            self.fail_next_call = False
            raise PaymentError("fake provider outage (scripted)")

    def _maybe_fail_key(self, idempotency_key: str) -> None:
        if idempotency_key in self.fail_keys:
            self.fail_keys.discard(idempotency_key)
            raise PaymentError(f"fake provider outage (scripted for {idempotency_key})")

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult:
        self._maybe_fail()
        return AccountResult(provider_account_id=f"acct_fake_{seller_id}", payments_ready=True)

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str:
        return f"https://fake.example/onboard/{provider_account_id}?return={return_url}"

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult:
        self._maybe_fail()
        self._maybe_fail_key(idempotency_key)
        status = self.next_charge_status
        self.next_charge_status = PaymentStatus.SUCCEEDED  # scripted statuses are one-shot
        n = next(self._seq)
        return ChargeResult(
            provider_payment_id=f"pay_fake_{n}",
            status=status,
            client_secret=None if status is PaymentStatus.SUCCEEDED else f"cs_fake_{n}",
        )

    def cancel_charge(self, provider_payment_id: str) -> None:
        self._maybe_fail()
        self.cancelled.append(provider_payment_id)

    def refund(
        self, provider_payment_id: str, *, idempotency_key: str, amount: Decimal | None = None
    ) -> RefundResult:
        self._maybe_fail()
        self._maybe_fail_key(idempotency_key)
        self.refunded.append(provider_payment_id)
        self.refund_keys.append(idempotency_key)
        self.refund_amounts.append(None if amount is None else str(amount))
        return RefundResult(provider_refund_id=f"re_fake_{provider_payment_id}")

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult:
        self.transfer_keys.append(idempotency_key)  # ATTEMPT-recorded — see class docstring
        self._maybe_fail()
        self._maybe_fail_key(idempotency_key)
        status = self.next_transfer_status
        self.next_transfer_status = PayoutStatus.PAID
        return TransferResult(provider_transfer_id=f"tr_fake_{next(self._seq)}", status=status)

    def reverse_transfer(
        self, provider_transfer_id: str, *, amount: Decimal, idempotency_key: str
    ) -> ReversalResult:
        self._maybe_fail()
        self._maybe_fail_key(idempotency_key)
        self.reversals.append((provider_transfer_id, str(amount), idempotency_key))
        return ReversalResult(provider_reversal_id=f"trr_fake_{len(self.reversals)}")

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent:
        data: dict[str, Any] = json.loads(payload)
        ready = data.get("payments_ready")
        amount_minor = data.get("amount_minor")
        return PaymentEvent(
            event_id=str(data["event_id"]),
            kind=str(data["kind"]),
            object_id=str(data["object_id"]),
            payments_ready=None if ready is None else bool(ready),
            amount_minor=None if amount_minor is None else int(amount_minor),
            outcome=None if data.get("outcome") is None else str(data["outcome"]),
            related_id=None if data.get("related_id") is None else str(data["related_id"]),
        )
