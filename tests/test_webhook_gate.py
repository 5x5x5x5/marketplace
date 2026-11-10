"""The unsigned webhook is closed in-app: the fake provider verifies no
signature, so its webhook route only exists when unsigned webhooks are
explicitly allowed, and a Stripe config that can't verify webhooks (or that
still allows unsigned ones) refuses to start."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from marketplace.db import SessionLocal
from marketplace.entities import WebhookEvent
from marketplace.settings import Settings, settings


def test_fake_mode_webhook_is_closed_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "allow_unsigned_webhooks", False)
    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt-forged", "kind": "payment_succeeded", "object_id": "pi_fake_1"},
    )
    assert r.status_code == 404
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_fake_mode_webhook_opens_when_allowed(client: TestClient) -> None:
    assert settings.allow_unsigned_webhooks  # the suite opts in via conftest
    r = client.post(
        "/v1/payments/webhook", json={"event_id": "evt-dev", "kind": "ignored", "object_id": "x"}
    )
    assert r.status_code == 200


def test_unsigned_webhooks_default_off() -> None:
    assert Settings.model_fields["allow_unsigned_webhooks"].default is False


def _stripe(*, webhook_secret: str, allow_unsigned: bool) -> Settings:
    return Settings(
        stripe_secret_key="sk_test_x",
        stripe_webhook_secret=webhook_secret,
        allow_unsigned_webhooks=allow_unsigned,
    )


def test_stripe_without_webhook_secret_is_refused() -> None:
    with pytest.raises(ValidationError, match="STRIPE_WEBHOOK_SECRET"):
        _stripe(webhook_secret="", allow_unsigned=False)


def test_stripe_with_unsigned_webhooks_allowed_is_refused() -> None:
    with pytest.raises(ValidationError, match="ALLOW_UNSIGNED_WEBHOOKS"):
        _stripe(webhook_secret="whsec_x", allow_unsigned=True)


def test_complete_stripe_config_is_valid() -> None:
    _stripe(webhook_secret="whsec_x", allow_unsigned=False)


def test_fake_mode_with_unsigned_allowed_is_valid() -> None:
    Settings(stripe_secret_key="", allow_unsigned_webhooks=True)
