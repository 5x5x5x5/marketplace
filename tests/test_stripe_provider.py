"""StripeProvider unit tests: webhook signature verification and event mapping.

No network — we construct a valid Stripe-Signature header with the same HMAC
scheme Stripe uses (t=<ts>,v1=hexdigest of "<ts>.<payload>")."""

import hashlib
import hmac
import json
import time

import pytest

from marketplace.payments.port import WebhookSignatureError
from marketplace.payments.stripe_provider import StripeProvider

SECRET = "whsec_test_secret"


def _signed(payload: bytes) -> str:
    ts = int(time.time())
    mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(kind: str, obj: dict[str, object]) -> bytes:
    return json.dumps(
        {"id": "evt_test_1", "object": "event", "type": kind, "data": {"object": obj}}
    ).encode()


@pytest.fixture
def provider() -> StripeProvider:
    return StripeProvider("sk_test_dummy", SECRET)


def test_empty_webhook_secret_fails_fast() -> None:
    """An empty secret would verify forged webhooks (HMAC with an empty key)."""
    with pytest.raises(RuntimeError):
        StripeProvider("sk_test_dummy", "")


def test_valid_signature_maps_payment_succeeded(provider: StripeProvider) -> None:
    payload = _event("payment_intent.succeeded", {"id": "pi_123"})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.event_id == "evt_test_1"
    assert event.kind == "payment_succeeded"
    assert event.object_id == "pi_123"


def test_account_updated_carries_readiness(provider: StripeProvider) -> None:
    payload = _event("account.updated", {"id": "acct_1", "payouts_enabled": True})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "account_updated"
    assert event.payments_ready is True


def test_unhandled_kinds_map_to_ignored(provider: StripeProvider) -> None:
    payload = _event("customer.created", {"id": "cus_1"})
    assert provider.parse_webhook(payload, _signed(payload)).kind == "ignored"


def test_bad_signature_rejected(provider: StripeProvider) -> None:
    payload = _event("payment_intent.succeeded", {"id": "pi_123"})
    with pytest.raises(WebhookSignatureError):
        provider.parse_webhook(payload, "t=1,v1=deadbeef")


def test_missing_signature_rejected(provider: StripeProvider) -> None:
    with pytest.raises(WebhookSignatureError):
        provider.parse_webhook(b"{}", None)


def test_chargeback_events_map_and_carry_fields(provider: StripeProvider) -> None:
    opened = json.dumps(
        {
            "id": "evt_cb1",
            "object": "event",
            "type": "charge.dispute.created",
            "data": {"object": {"id": "dp_1", "payment_intent": "pi_9", "amount": 8000}},
        }
    ).encode()
    event = provider.parse_webhook(opened, _signed(opened))
    assert event.kind == "chargeback_opened"
    assert event.object_id == "dp_1"
    assert event.related_id == "pi_9"
    assert event.amount_minor == 8000

    closed = json.dumps(
        {
            "id": "evt_cb2",
            "object": "event",
            "type": "charge.dispute.closed",
            "data": {
                "object": {"id": "dp_1", "payment_intent": "pi_9", "amount": 8000, "status": "lost"}
            },
        }
    ).encode()
    event = provider.parse_webhook(closed, _signed(closed))
    assert event.kind == "chargeback_closed"
    assert event.outcome == "lost"


def test_dispute_event_without_payment_intent_has_no_related_id(provider: StripeProvider) -> None:
    """related_id is PaymentIntent-only: the consumer matches it against
    Payment.provider_payment_id (always a PI id), so a bare charge id could
    never match — carrying it just manufactured a misleading 'unknown charge'
    lookup."""
    payload = _event("charge.dispute.created", {"id": "dp_2", "charge": "ch_1", "amount": 500})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "chargeback_opened"
    assert event.related_id is None
    assert event.amount_minor == 500


def test_partial_transfer_reversal_is_ignored_not_failed(provider: StripeProvider) -> None:
    """Dispute clawbacks create PARTIAL reversals — Stripe fires
    transfer.reversed for those too, but the object's `reversed` field stays
    False and the payout is still paid. Only a FULLY reversed transfer means
    the transfer itself failed."""
    payload = _event("transfer.reversed", {"id": "tr_1", "reversed": False, "amount_reversed": 400})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "ignored"


def test_full_transfer_reversal_maps_to_transfer_failed(provider: StripeProvider) -> None:
    payload = _event("transfer.reversed", {"id": "tr_1", "reversed": True, "amount_reversed": 1400})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "transfer_failed"
