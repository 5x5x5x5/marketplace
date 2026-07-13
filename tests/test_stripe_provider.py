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
