"""Replay-safety audit of `_apply_payment_event`, one section per branch.

The outbox retry job re-applies stored events, so every branch must be safe
to apply TWICE (the same economic event again, under another event id or a
straight re-run, as `replay` does) and safe to apply LATE (the first apply
failed, the job or payment moved on, and only then the retry job ran it, as
`defer` + `retry_now` does)."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from marketplace.db import SessionLocal
from marketplace.entities import Adjustment, Dispute, Job, Payment, SellerProfile, WebhookEvent
from marketplace.models import (
    DisputeStatus,
    EventKind,
    JobStatus,
    PaymentStatus,
    PayoutStatus,
    WebhookEventStatus,
)
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import PaymentEvent
from tests.conftest import AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail, pending_accept
from tests.webhook_helpers import (
    completed_job,
    defer,
    notification_count,
    payout_for,
    post_event,
    replay,
    retry_now,
    row,
)


def _job_state(job_id: str) -> tuple[JobStatus, PaymentStatus]:
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        return job.status, payment.status


def _payments_ready(seller_id: str) -> bool:
    with SessionLocal() as s:
        prof = s.get(SellerProfile, seller_id)
        assert prof is not None
        return prof.payments_ready


def _dispute() -> Dispute:
    with SessionLocal() as s:
        [dispute] = s.scalars(select(Dispute)).all()
        return dispute


def _adjustments() -> list[str]:
    with SessionLocal() as s:
        return sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())


def _opened(event_id: str, dp: str, pid: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "kind": "chargeback_opened",
        "object_id": dp,
        "related_id": pid,
        "amount_minor": 2000,
    }


def _closed(event_id: str, dp: str, pid: str, outcome: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "kind": "chargeback_closed",
        "object_id": dp,
        "related_id": pid,
        "amount_minor": 2000,
        "outcome": outcome,
    }


# ---------- payment_succeeded ----------


def test_payment_succeeded_twice_is_one_transition(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    post_event(client, event_id="evt_s1", kind="payment_succeeded", object_id=pid)
    replay(PaymentEvent(event_id="evt_s2", kind="payment_succeeded", object_id=pid))
    assert _job_state(job_id) == (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED)


def test_payment_succeeded_twice_after_completion_leaves_the_job_alone(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id, pid = completed_job(client, auth, basic_service)
    replay(PaymentEvent(event_id="evt_s_done", kind="payment_succeeded", object_id=pid))
    assert _job_state(job_id) == (JobStatus.COMPLETED, PaymentStatus.SUCCEEDED)


def test_payment_succeeded_late_after_buyer_cancel_never_resurrects(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    defer(client, monkeypatch, event_id="evt_late_s", kind="payment_succeeded", object_id=pid)
    assert (
        client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice")).status_code == 200
    )

    retry_now()

    assert row("evt_late_s").status is WebhookEventStatus.APPLIED  # consumed, as a no-op
    assert _job_state(job_id) == (JobStatus.CANCELLED, PaymentStatus.FAILED)


def test_payment_succeeded_late_after_refund_stays_refunded(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # instant success
    job_id = str(job["id"])
    with SessionLocal() as s:
        pid = s.scalar(select(Payment.provider_payment_id).where(Payment.job_id == UUID(job_id)))
    assert pid is not None
    defer(client, monkeypatch, event_id="evt_late_r", kind="payment_succeeded", object_id=pid)
    assert client.post(f"/v1/admin/jobs/{job_id}/cancel", headers=admin).status_code == 200

    retry_now()

    assert _job_state(job_id) == (JobStatus.CANCELLED, PaymentStatus.REFUNDED)


# ---------- payment_failed ----------


def test_payment_failed_twice_keeps_waiting(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    post_event(client, event_id="evt_f1", kind="payment_failed", object_id=pid)
    replay(PaymentEvent(event_id="evt_f2", kind="payment_failed", object_id=pid))
    assert _job_state(job_id) == (JobStatus.AWAITING_PAYMENT, PaymentStatus.FAILED)


def test_payment_failed_late_after_success_never_undoes_it(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    defer(client, monkeypatch, event_id="evt_late_f", kind="payment_failed", object_id=pid)
    post_event(client, event_id="evt_ok", kind="payment_succeeded", object_id=pid)

    retry_now()

    assert row("evt_late_f").status is WebhookEventStatus.APPLIED
    assert _job_state(job_id) == (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED)


# ---------- account_updated ----------


def test_account_updated_twice_is_idempotent(client: TestClient, auth: AuthFactory) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    fields = {"kind": "account_updated", "object_id": "acct_fake_s1", "payments_ready": False}
    post_event(client, event_id="evt_a1", **fields)
    replay(
        PaymentEvent(
            event_id="evt_a2",
            kind="account_updated",
            object_id="acct_fake_s1",
            payments_ready=False,
        )
    )
    assert _payments_ready("s1") is False


def test_account_updated_late_never_overwrites_a_newer_update(
    client: TestClient, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    defer(
        client,
        monkeypatch,
        event_id="evt_old",
        kind="account_updated",
        object_id="acct_fake_s1",
        payments_ready=False,
    )
    post_event(
        client,
        event_id="evt_new",
        kind="account_updated",
        object_id="acct_fake_s1",
        payments_ready=True,
    )

    retry_now()

    assert row("evt_old").status is WebhookEventStatus.APPLIED  # consumed, superseded
    assert _payments_ready("s1") is True


def test_account_updated_late_still_applies_when_nothing_newer_landed(
    client: TestClient, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    defer(
        client,
        monkeypatch,
        event_id="evt_only",
        kind="account_updated",
        object_id="acct_fake_s1",
        payments_ready=False,
    )
    retry_now()
    assert _payments_ready("s1") is False


# ---------- transfer_failed ----------


def test_transfer_failed_twice_pages_admins_once(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    post_event(client, event_id="evt_tf1", kind="transfer_failed", object_id=tr)
    replay(PaymentEvent(event_id="evt_tf2", kind="transfer_failed", object_id=tr))
    assert payout_for(job_id).status is PayoutStatus.FAILED
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 1


def test_transfer_failed_late_still_marks_and_pages_once(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    defer(client, monkeypatch, event_id="evt_tf_late", kind="transfer_failed", object_id=tr)
    assert payout_for(job_id).status is PayoutStatus.PAID  # nothing applied yet

    retry_now()

    assert payout_for(job_id).status is PayoutStatus.FAILED
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 1


def test_transfer_failed_late_for_a_replaced_transfer_is_a_noop(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an admin retry moved the payout onto a new transfer, a late
    failure for the old transfer id must not fail the new, paid one."""
    job_id, _pid = completed_job(client, auth, basic_service)
    payout = payout_for(job_id)
    old_tr = payout.provider_transfer_id
    assert old_tr is not None
    post_event(client, event_id="evt_tf_real", kind="transfer_failed", object_id=old_tr)
    r = client.post(f"/v1/admin/payouts/{payout.id}/retry", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "paid"
    defer(client, monkeypatch, event_id="evt_tf_stale", kind="transfer_failed", object_id=old_tr)

    retry_now()

    after = payout_for(job_id)
    assert after.status is PayoutStatus.PAID
    assert after.provider_transfer_id != old_tr
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 1


# ---------- transfer_paid ----------


def _pending_payout(client: TestClient, auth: AuthFactory, sid: str, fake: FakeProvider) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    fake.next_transfer_status = PayoutStatus.PENDING
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    assert payout_for(str(job["id"])).status is PayoutStatus.PENDING
    return str(job["id"])


def test_transfer_paid_twice_is_idempotent(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id = _pending_payout(client, auth, basic_service, fake_payments)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    post_event(client, event_id="evt_tp1", kind="transfer_paid", object_id=tr)
    replay(PaymentEvent(event_id="evt_tp2", kind="transfer_paid", object_id=tr))
    assert payout_for(job_id).status is PayoutStatus.PAID


def test_transfer_paid_late_after_failure_never_flips_back_to_paid(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _pending_payout(client, auth, basic_service, fake_payments)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    defer(client, monkeypatch, event_id="evt_tp_late", kind="transfer_paid", object_id=tr)
    post_event(client, event_id="evt_tf", kind="transfer_failed", object_id=tr)

    retry_now()

    assert row("evt_tp_late").status is WebhookEventStatus.APPLIED
    assert payout_for(job_id).status is PayoutStatus.FAILED  # the admin can still retry it


# ---------- chargeback_opened ----------


def test_chargeback_opened_twice_records_and_notifies_once(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o1", "dp_1", pid))
    replay(
        PaymentEvent(
            event_id="evt_o2",
            kind="chargeback_opened",
            object_id="dp_1",
            related_id=pid,
            amount_minor=2000,
        )
    )
    assert _dispute().provider_dispute_id == "dp_1"
    assert notification_count(EventKind.CHARGEBACK_OPENED_ADMIN) == 1


def test_chargeback_opened_replay_after_a_newer_chargeback_does_not_reannotate(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o1", "dp_1", pid))
    post_event(client, **_opened("evt_o2", "dp_2", pid))
    replay(
        PaymentEvent(
            event_id="evt_o1_again",
            kind="chargeback_opened",
            object_id="dp_1",
            related_id=pid,
            amount_minor=2000,
        )
    )
    assert _dispute().provider_dispute_id == "dp_2"
    assert notification_count(EventKind.CHARGEBACK_OPENED_ADMIN) == 2


def test_chargeback_opened_late_after_its_close_changes_nothing(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close is applied before the open (the open was deferred): the close
    maps through the payment and books the loss, and the late open is then a
    no-op rather than a second notice or a reset dispute."""
    _job_id, pid = completed_job(client, auth, basic_service)
    defer(client, monkeypatch, **_opened("evt_o_late", "dp_1", pid))
    post_event(client, **_closed("evt_c", "dp_1", pid, "lost"))
    assert _dispute().status is DisputeStatus.CHARGEBACK_LOST
    assert _adjustments() == ["chargeback_fee", "chargeback_loss"]

    retry_now()

    assert row("evt_o_late").status is WebhookEventStatus.APPLIED
    dispute = _dispute()
    assert (dispute.provider_dispute_id, dispute.status) == ("dp_1", DisputeStatus.CHARGEBACK_LOST)
    assert _adjustments() == ["chargeback_fee", "chargeback_loss"]
    assert notification_count(EventKind.CHARGEBACK_OPENED_ADMIN) == 0
    assert notification_count(EventKind.CHARGEBACK_CLOSED_ADMIN) == 1


# ---------- chargeback_closed ----------


def test_chargeback_lost_twice_books_once(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o", "dp_1", pid))
    post_event(client, **_closed("evt_c1", "dp_1", pid, "lost"))
    replay(
        PaymentEvent(
            event_id="evt_c2",
            kind="chargeback_closed",
            object_id="dp_1",
            related_id=pid,
            amount_minor=2000,
            outcome="lost",
        )
    )
    assert _adjustments() == ["chargeback_fee", "chargeback_loss"]
    assert notification_count(EventKind.CHARGEBACK_CLOSED_ADMIN) == 1


def test_chargeback_lost_replay_books_once_even_after_ledger_retention(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """The money marker is the fee row itself, not the reapable webhook
    ledger: a replay after retention emptied the ledger still books nothing."""
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o", "dp_1", pid))
    post_event(client, **_closed("evt_c1", "dp_1", pid, "lost"))
    with SessionLocal() as s:
        s.execute(delete(WebhookEvent))
        s.commit()
    replay(
        PaymentEvent(
            event_id="evt_c2",
            kind="chargeback_closed",
            object_id="dp_1",
            related_id=pid,
            amount_minor=2000,
            outcome="lost",
        )
    )
    assert _adjustments() == ["chargeback_fee", "chargeback_loss"]


def test_chargeback_won_twice_notifies_once(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o", "dp_1", pid))
    post_event(client, **_closed("evt_c1", "dp_1", pid, "won"))
    replay(
        PaymentEvent(
            event_id="evt_c2",
            kind="chargeback_closed",
            object_id="dp_1",
            related_id=pid,
            outcome="won",
        )
    )
    assert _dispute().status is DisputeStatus.CHARGEBACK_WON
    assert _adjustments() == []
    assert notification_count(EventKind.CHARGEBACK_CLOSED_ADMIN) == 1


def test_chargeback_won_on_a_second_dispute_object_is_not_a_replay(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = completed_job(client, auth, basic_service)
    post_event(client, **_opened("evt_o1", "dp_1", pid))
    post_event(client, **_closed("evt_c1", "dp_1", pid, "won"))
    post_event(client, **_opened("evt_o2", "dp_2", pid))
    post_event(client, **_closed("evt_c2", "dp_2", pid, "won"))
    assert notification_count(EventKind.CHARGEBACK_CLOSED_ADMIN) == 2


def test_chargeback_lost_late_after_arbitration_keeps_ruling_and_books_once(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = completed_job(client, auth, basic_service)
    r = client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "not done"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 201
    post_event(client, **_opened("evt_o", "dp_1", pid))
    defer(client, monkeypatch, **_closed("evt_c_late", "dp_1", pid, "lost"))
    r = client.post(
        f"/v1/admin/disputes/{_dispute().id}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "0.00", "note": "arbitrated"},
        headers=admin,
    )
    assert r.status_code == 200, r.text

    retry_now()
    retry_now()  # nothing left due: a second pass is a no-op

    assert _dispute().status is DisputeStatus.RESOLVED
    assert _adjustments() == ["chargeback_fee", "chargeback_loss", "refund"]


def test_chargeback_closed_without_a_mappable_payment_is_a_noop(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    completed_job(client, auth, basic_service)
    post_event(client, **_closed("evt_c_x", "dp_x", "pay_unknown", "lost"))
    with SessionLocal() as s:
        assert s.scalar(select(Dispute)) is None
    assert _adjustments() == []
