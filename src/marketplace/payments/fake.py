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
    TransferResult,
)


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self._seq = count(1)
        self.next_charge_status: PaymentStatus = PaymentStatus.SUCCEEDED
        self.next_transfer_status: PayoutStatus = PayoutStatus.PAID
        self.fail_next_call: bool = False
        self.cancelled: list[str] = []
        self.refunded: list[str] = []

    def reset(self) -> None:
        self.next_charge_status = PaymentStatus.SUCCEEDED
        self.next_transfer_status = PayoutStatus.PAID
        self.fail_next_call = False
        self.cancelled.clear()
        self.refunded.clear()

    def _maybe_fail(self) -> None:
        if self.fail_next_call:
            self.fail_next_call = False
            raise PaymentError("fake provider outage (scripted)")

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
        status = self.next_charge_status
        self.next_charge_status = PaymentStatus.SUCCEEDED  # scripted statuses are one-shot
        n = next(self._seq)
        return ChargeResult(
            provider_payment_id=f"pay_fake_{n}",
            status=status,
            client_secret=None if status is PaymentStatus.SUCCEEDED else f"cs_fake_{n}",
        )

    def cancel_charge(self, provider_payment_id: str) -> None:
        self.cancelled.append(provider_payment_id)

    def refund(self, provider_payment_id: str, *, idempotency_key: str) -> RefundResult:
        self._maybe_fail()
        self.refunded.append(provider_payment_id)
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
        self._maybe_fail()
        status = self.next_transfer_status
        self.next_transfer_status = PayoutStatus.PAID
        return TransferResult(provider_transfer_id=f"tr_fake_{next(self._seq)}", status=status)

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent:
        data: dict[str, Any] = json.loads(payload)
        ready = data.get("payments_ready")
        return PaymentEvent(
            event_id=str(data["event_id"]),
            kind=str(data["kind"]),
            object_id=str(data["object_id"]),
            payments_ready=None if ready is None else bool(ready),
        )
