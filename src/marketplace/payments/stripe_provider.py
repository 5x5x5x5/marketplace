"""Stripe adapter: controller-properties connected accounts (the legacy
Standard/Express/Custom types are deprecated), PaymentIntents for buyer
charges, Transfers for payouts, signed webhooks.

Not exercised against live Stripe in this repo (no account on the dev box):
the signature/parse path is unit-tested; the API calls follow current docs.
Run against a Stripe test account before real money."""

from decimal import Decimal
from typing import Any, cast

import stripe

from ..models import PaymentStatus, PayoutStatus
from .port import (
    AccountResult,
    ChargeResult,
    PaymentError,
    PaymentEvent,
    RefundResult,
    ReversalResult,
    TransferResult,
    WebhookSignatureError,
    to_minor_units,
)

# PaymentIntent statuses that are terminal for us; everything else
# (requires_payment_method, requires_confirmation, processing, …) is PENDING.
_PI_STATUS = {"succeeded": PaymentStatus.SUCCEEDED, "canceled": PaymentStatus.FAILED}

_EVENT_KINDS = {
    "payment_intent.succeeded": "payment_succeeded",
    "payment_intent.payment_failed": "payment_failed",
    "account.updated": "account_updated",
    "transfer.reversed": "transfer_failed",
    "charge.dispute.created": "chargeback_opened",
    "charge.dispute.closed": "chargeback_closed",
}


class StripeProvider:
    name = "stripe"

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        # An empty secret would make construct_event compute HMACs with an empty
        # key — forged webhooks would pass "verification". Fail fast instead.
        if not webhook_secret:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET is required when STRIPE_SECRET_KEY is set")
        self._client = stripe.StripeClient(secret_key)
        self._webhook_secret = webhook_secret

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult:
        try:
            acct = self._client.v1.accounts.create(
                params={
                    "controller": {
                        "fees": {"payer": "application"},
                        "losses": {"payments": "application"},
                        "stripe_dashboard": {"type": "express"},
                    },
                    "capabilities": {"transfers": {"requested": True}},
                    "metadata": {"seller_id": seller_id},
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return AccountResult(provider_account_id=acct.id, payments_ready=bool(acct.payouts_enabled))

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str:
        try:
            link = self._client.v1.account_links.create(
                params={
                    "account": provider_account_id,
                    "refresh_url": return_url,
                    "return_url": return_url,
                    "type": "account_onboarding",
                }
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return link.url

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult:
        try:
            pi = self._client.v1.payment_intents.create(
                params={
                    "amount": to_minor_units(amount),
                    "currency": currency,
                    "automatic_payment_methods": {"enabled": True},
                    "metadata": {"job_id": job_id, "buyer_id": buyer_id},
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return ChargeResult(
            provider_payment_id=pi.id,
            status=_PI_STATUS.get(pi.status, PaymentStatus.PENDING),
            client_secret=pi.client_secret,
        )

    def cancel_charge(self, provider_payment_id: str) -> None:
        try:
            self._client.v1.payment_intents.cancel(provider_payment_id)
        except stripe.InvalidRequestError as exc:
            # Stripe errors when cancelling an already-canceled PI. A void that
            # succeeded before a DB rollback must stay retryable, so treat
            # "already canceled" as success instead of wedging the job forever.
            try:
                pi = self._client.v1.payment_intents.retrieve(provider_payment_id)
            except stripe.StripeError as retrieve_exc:
                raise PaymentError(str(retrieve_exc)) from retrieve_exc
            if pi.status != "canceled":  # already-voided is success; anything else is real
                raise PaymentError(str(exc)) from exc
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc

    def refund(
        self, provider_payment_id: str, *, idempotency_key: str, amount: Decimal | None = None
    ) -> RefundResult:
        try:
            re = self._client.v1.refunds.create(
                params={"payment_intent": provider_payment_id}
                if amount is None
                else {"payment_intent": provider_payment_id, "amount": to_minor_units(amount)},
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return RefundResult(provider_refund_id=re.id)

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult:
        try:
            tr = self._client.v1.transfers.create(
                params={
                    "amount": to_minor_units(amount),
                    "currency": currency,
                    "destination": provider_account_id,
                    "transfer_group": job_id,
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        # Transfers settle synchronously (platform balance → connected balance).
        return TransferResult(provider_transfer_id=tr.id, status=PayoutStatus.PAID)

    def reverse_transfer(
        self, provider_transfer_id: str, *, amount: Decimal, idempotency_key: str
    ) -> ReversalResult:
        try:
            reversal = self._client.v1.transfers.reversals.create(
                provider_transfer_id,
                params={"amount": to_minor_units(amount)},
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return ReversalResult(provider_reversal_id=reversal.id)

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent:
        if signature is None:
            raise WebhookSignatureError("missing Stripe-Signature header")
        try:
            # construct_event's stub is unannotated in the installed SDK (stub
            # churn between versions), so pyright sees its type as partially
            # unknown; cast the member access (not the result) to keep `event`
            # typed as stripe.Event for everything downstream.
            event: stripe.Event = cast(Any, stripe.Webhook).construct_event(
                payload, signature, self._webhook_secret
            )
        except stripe.SignatureVerificationError as exc:
            raise WebhookSignatureError(str(exc)) from exc
        except ValueError as exc:
            raise PaymentError(f"malformed payload: {exc}") from exc
        # dict(event.data.object) fails on this SDK's StripeObject: it falls back
        # to the legacy sequence-iteration protocol (int-indexed __getitem__)
        # instead of the mapping protocol. to_dict() is the SDK's own escape hatch.
        obj: dict[str, Any] = event.data.object.to_dict()
        ready: bool | None = None
        if event.type == "account.updated":
            ready = bool(obj.get("payouts_enabled", False))
        amount_minor: int | None = None
        outcome: str | None = None
        related_id: str | None = None
        if event.type.startswith("charge.dispute."):
            raw_amount = obj.get("amount")
            amount_minor = None if raw_amount is None else int(raw_amount)
            related_id = str(obj.get("payment_intent") or obj.get("charge") or "") or None
            if event.type == "charge.dispute.closed":
                outcome = str(obj.get("status", "")) or None
        return PaymentEvent(
            event_id=event.id,
            kind=_EVENT_KINDS.get(event.type, "ignored"),
            object_id=str(obj.get("id", "")),
            payments_ready=ready,
            amount_minor=amount_minor,
            outcome=outcome,
            related_id=related_id,
        )
