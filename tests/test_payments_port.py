"""Pure unit tests for the payment port and the deterministic fake provider."""

from decimal import Decimal

import pytest

from marketplace.models import PaymentStatus, PayoutStatus, to_money
from marketplace.payments import fake_provider, get_provider
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import ChargeResult, PaymentError, to_minor_units


def _charge(fake: FakeProvider) -> ChargeResult:
    return fake.charge_buyer(
        buyer_id="alice",
        amount=Decimal("10.00"),
        currency="usd",
        job_id="job-1",
        idempotency_key="charge:job-1",
    )


def test_to_minor_units() -> None:
    assert to_minor_units(to_money("12.34")) == 1234
    assert to_minor_units(to_money(0)) == 0
    assert to_minor_units(to_money("0.05")) == 5
    assert to_minor_units(to_money("19.999")) == 2000  # to_money already rounded half-up


def test_default_provider_is_the_fake_singleton() -> None:
    assert get_provider() is fake_provider


def test_fake_charge_succeeds_instantly_by_default() -> None:
    fake = FakeProvider()
    result = _charge(fake)
    assert result.status is PaymentStatus.SUCCEEDED
    assert result.client_secret is None
    assert result.provider_payment_id.startswith("pay_fake_")


def test_fake_scripted_pending_is_one_shot() -> None:
    fake = FakeProvider()
    fake.next_charge_status = PaymentStatus.PENDING
    first = _charge(fake)
    second = _charge(fake)
    assert first.status is PaymentStatus.PENDING
    assert first.client_secret is not None
    assert second.status is PaymentStatus.SUCCEEDED


def test_fake_outage_raises_once_then_recovers() -> None:
    fake = FakeProvider()
    fake.fail_next_call = True
    with pytest.raises(PaymentError):
        _charge(fake)
    assert _charge(fake).status is PaymentStatus.SUCCEEDED


def test_fake_onboarding_is_instantly_ready() -> None:
    fake = FakeProvider()
    acct = fake.create_seller_account("bob", idempotency_key="acct:bob")
    assert acct.payments_ready is True
    assert "bob" in acct.provider_account_id
    assert acct.provider_account_id in fake.onboarding_link(acct.provider_account_id, "http://x")


def test_fake_transfer_and_refund_and_cancel() -> None:
    fake = FakeProvider()
    tr = fake.transfer_to_seller(
        provider_account_id="acct_fake_bob",
        amount=Decimal("14.00"),
        currency="usd",
        job_id="job-1",
        idempotency_key="transfer:job-1",
    )
    assert tr.status is PayoutStatus.PAID
    fake.next_transfer_status = PayoutStatus.FAILED
    tr2 = fake.transfer_to_seller(
        provider_account_id="acct_fake_bob",
        amount=Decimal("14.00"),
        currency="usd",
        job_id="job-2",
        idempotency_key="transfer:job-2",
    )
    assert tr2.status is PayoutStatus.FAILED
    fake.refund("pay_fake_1", idempotency_key="refund:job-1")
    assert fake.refunded == ["pay_fake_1"]
    fake.cancel_charge("pay_fake_2")
    assert fake.cancelled == ["pay_fake_2"]


def test_fake_parses_unsigned_json_webhooks() -> None:
    fake = FakeProvider()
    event = fake.parse_webhook(
        b'{"event_id": "evt_1", "kind": "payment_succeeded", "object_id": "pay_fake_1"}', None
    )
    assert event.event_id == "evt_1"
    assert event.kind == "payment_succeeded"
    assert event.object_id == "pay_fake_1"
    assert event.payments_ready is None


def test_payment_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"payments", "payouts", "webhook_events", "idempotency_keys"} <= set(
        Base.metadata.tables
    )


def test_fake_partial_refund_records_amount_and_key() -> None:
    fake = FakeProvider()
    fake.refund("pay_fake_1", idempotency_key="refund:j:dispute", amount=Decimal("30.00"))
    fake.refund("pay_fake_2", idempotency_key="refund:j2")  # full refund, no amount
    assert fake.refunded == ["pay_fake_1", "pay_fake_2"]
    assert fake.refund_keys == ["refund:j:dispute", "refund:j2"]
    assert fake.refund_amounts == ["30.00", None]


def test_fake_reverse_transfer_records_and_fails_scriptably() -> None:
    fake = FakeProvider()
    result = fake.reverse_transfer(
        "tr_fake_1", amount=Decimal("20.00"), idempotency_key="reversal:j:dispute"
    )
    assert result.provider_reversal_id.startswith("trr_fake_")
    assert fake.reversals == [("tr_fake_1", "20.00", "reversal:j:dispute")]
    fake.fail_next_call = True
    with pytest.raises(PaymentError):
        fake.reverse_transfer("tr_fake_1", amount=Decimal("1.00"), idempotency_key="k")


def test_fake_webhook_passes_chargeback_fields_through() -> None:
    fake = FakeProvider()
    event = fake.parse_webhook(
        b'{"event_id": "evt_cb", "kind": "chargeback_opened", "object_id": "dp_1",'
        b' "related_id": "pay_fake_9", "amount_minor": 8000, "outcome": null}',
        None,
    )
    assert event.related_id == "pay_fake_9"
    assert event.amount_minor == 8000
    assert event.outcome is None
