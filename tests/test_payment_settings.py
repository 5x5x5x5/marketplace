"""Startup refuses payment configurations that cannot work or would leak."""

import pytest
from pydantic import ValidationError

from marketplace.models import PaymentStatus
from marketplace.payments.fake import FakeProvider
from marketplace.settings import Settings, settings


def _stripe(**over: object) -> Settings:
    fields: dict[str, object] = {
        "stripe_secret_key": "sk_test_x",
        "stripe_webhook_secret": "whsec_x",
        "stripe_publishable_key": "pk_test_x",
        "allow_unsigned_webhooks": False,
        "fake_charges_pending": False,
    } | over
    return Settings(**fields)  # pyright: ignore[reportArgumentType]


def test_complete_stripe_config_is_valid() -> None:
    _stripe()


def test_stripe_without_publishable_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="STRIPE_PUBLISHABLE_KEY"):
        _stripe(stripe_publishable_key="")


@pytest.mark.parametrize("key", ["sk_test_secret", "rk_test_restricted", "whsec_x", "nonsense"])
def test_a_publishable_key_that_is_not_pk_is_refused(key: str) -> None:
    # A secret key pasted here would be published in every payment page.
    with pytest.raises(ValidationError, match="pk_"):
        _stripe(stripe_publishable_key=key)


def test_fake_pending_mode_is_refused_with_stripe() -> None:
    with pytest.raises(ValidationError, match="FAKE_CHARGES_PENDING"):
        _stripe(fake_charges_pending=True)


def test_new_payment_settings_default_off() -> None:
    assert Settings.model_fields["stripe_publishable_key"].default == ""
    assert Settings.model_fields["fake_charges_pending"].default is False


def test_fake_has_no_browser_step() -> None:
    assert FakeProvider().browser_key is None


def test_fake_pending_mode_leaves_charges_pending(
    fake_payments: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    monkeypatch.setattr(settings, "fake_charges_pending", True)
    for job in ("j1", "j2"):  # every charge, not a one-shot
        charge = fake_payments.charge_buyer(
            buyer_id="b",
            amount=Decimal("1.00"),
            currency="usd",
            job_id=job,
            idempotency_key=f"charge:{job}",
        )
        assert charge.status is PaymentStatus.PENDING
        assert charge.client_secret is not None
