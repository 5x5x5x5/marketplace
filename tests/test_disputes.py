"""Disputes, arbitration, adjustments ledger, chargebacks."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Adjustment, Dispute, Job, Payment, WebhookEvent
from marketplace.mail import RecordingEmailSender
from marketplace.models import AdjustmentKind, DisputeStatus, PayoutStatus
from marketplace.notifications import drain_once
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import ReversalResult
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def test_dispute_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"disputes", "adjustments"} <= set(Base.metadata.tables)


def _drain() -> RecordingEmailSender:
    recorder = RecordingEmailSender()
    drain_once(recorder)
    return recorder


def _completed_job(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    return str(job["id"])


def _open_dispute(client: TestClient, auth: AuthFactory, job_id: str) -> dict[str, object]:
    r = client.post(
        f"/v1/jobs/{job_id}/dispute",
        json={"reason": "work was not as described"},
        headers=auth("buyer", "alice"),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_open_dispute_happy_path_and_views(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _drain()  # clear lifecycle mail
    body = _open_dispute(client, auth, job_id)
    assert body["status"] == "open"
    assert "clawback_amount" not in body  # buyer never sees seller money

    seller_view = client.get(
        f"/v1/seller/jobs/{job_id}/dispute", headers=auth("seller", "s1")
    ).json()
    assert seller_view["reason"] == "work was not as described"
    assert "refund_amount" not in seller_view  # seller never sees buyer money

    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert len(queue) == 1 and queue[0]["source"] == "buyer"

    recorder = _drain()
    seller_mail = [m for m in recorder.sent if "s1@" in m[0]]
    admin_mail = [m for m in recorder.sent if "ops@" in m[0]]
    assert seller_mail and "not as described" in seller_mail[0][2]
    assert admin_mail  # arbitration ping


def test_open_dispute_guards(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    # Not completed yet -> 409.
    r = client.post(
        f"/v1/jobs/{job['id']}/dispute", json={"reason": "x"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409
    # Someone else's job -> 404.
    r = client.post(
        f"/v1/jobs/{job['id']}/dispute", json={"reason": "x"}, headers=auth("buyer", "bob")
    )
    assert r.status_code == 404


def test_dispute_window_and_duplicate_guards(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _open_dispute(client, auth, job_id)
    r = client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "again"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409  # one dispute per job

    job2_id = _completed_job(client, auth, basic_service)
    with SessionLocal() as s:
        job2 = s.get(Job, UUID(job2_id))
        assert job2 is not None
        job2.completed_at = datetime.now(UTC) - timedelta(days=8)
        s.commit()
    r = client.post(
        f"/v1/jobs/{job2_id}/dispute", json={"reason": "late"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409  # window elapsed


def _resolve(
    client: TestClient, admin: Header, dispute_id: str, refund: str, clawback: str
) -> dict[str, object]:
    r = client.post(
        f"/v1/admin/disputes/{dispute_id}/resolve",
        json={"refund_amount": refund, "clawback_amount": clawback, "note": "arbitrated"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_partial_resolution_moves_money_and_ledgers(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)
    _drain()

    resolved = _resolve(client, admin, str(dispute["id"]), "6.00", "4.00")
    assert resolved["status"] == "resolved"
    assert resolved["refund_amount"] == "6.00" and resolved["clawback_amount"] == "4.00"

    # Provider legs used the dispute-specific keys and partial amounts.
    assert fake_payments.refund_keys[-1] == f"refund:{job_id}:dispute"
    assert fake_payments.refund_amounts[-1] == "6.00"
    assert fake_payments.reversals[-1][1] == "4.00"
    assert fake_payments.reversals[-1][2] == f"reversal:{job_id}:dispute"

    with SessionLocal() as s:
        kinds = {a.kind.value: str(a.amount) for a in s.scalars(select(Adjustment)).all()}
    assert kinds == {"refund": "6.00", "clawback": "4.00"}

    # Margin: gross unchanged (14->20 job = 6.00 margin); net = 6 - 6 + 4 = 4.00.
    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert summary["platform_margin"] == "6.00"
    assert summary["adjustments_net"] == "-2.00"
    assert summary["platform_margin_net"] == "4.00"

    # Both parties notified, asymmetrically.
    recorder = _drain()
    buyer_body = next(m[2] for m in recorder.sent if "alice@" in m[0])
    seller_body = next(m[2] for m in recorder.sent if "s1@" in m[0])
    assert "6.00" in buyer_body and "4.00" not in buyer_body
    assert "4.00" in seller_body and "6.00" not in seller_body


def test_reject_resolution_moves_nothing(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)
    before_refunds = len(fake_payments.refunded)
    resolved = _resolve(client, admin, str(dispute["id"]), "0.00", "0.00")
    assert resolved["status"] == "resolved"
    assert len(fake_payments.refunded) == before_refunds  # no provider calls
    assert fake_payments.reversals == []
    with SessionLocal() as s:
        assert s.scalar(select(Adjustment)) is None  # no ledger noise
    # Second resolve -> 409.
    r = client.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "1.00", "clawback_amount": "0.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 409


def test_resolution_bounds_and_convergence(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)

    # Out of bounds -> 422, nothing recorded.
    r = client.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "999.00", "clawback_amount": "0.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 422

    # Provider outage at the first leg -> 502, dispute stays open, no adjustments.
    fake_payments.fail_next_call = True
    r = client.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 502
    with SessionLocal() as s:
        assert s.scalar(select(Adjustment)) is None
    # Retry converges: same keys replay the succeeded leg.
    resolved = _resolve(client, admin, str(dispute["id"]), "6.00", "4.00")
    assert resolved["status"] == "resolved"


def test_partial_failure_retry_replays_succeeded_leg_by_key(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)

    # Refund succeeds, then the reversal leg fails.
    fake_payments.fail_keys = {f"reversal:{job_id}:dispute"}
    r = client.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 502
    with SessionLocal() as s:
        assert s.scalar(select(Adjustment)) is None  # nothing ledgered mid-flight
    assert len(fake_payments.refund_keys) == 1  # the refund DID execute at the provider

    # Retry converges: the refund leg replays the SAME key (a no-op on real
    # Stripe), the reversal completes, and the ledger lands exactly once.
    resolved = _resolve(client, admin, str(dispute["id"]), "6.00", "4.00")
    assert resolved["status"] == "resolved"
    assert fake_payments.refund_keys == [
        f"refund:{job_id}:dispute",
        f"refund:{job_id}:dispute",
    ]  # two calls, identical key => provider-side replay, not a second refund
    assert len(fake_payments.reversals) == 1
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["clawback", "refund"]


def _paid_job_pid(client: TestClient, auth: AuthFactory, sid: str) -> tuple[str, str]:
    job_id = _completed_job(client, auth, sid)
    with SessionLocal() as s:
        pid = s.scalar(select(Payment.provider_payment_id).where(Payment.job_id == UUID(job_id)))
    assert pid is not None
    return job_id, pid


def test_chargeback_opened_creates_provider_dispute(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = _paid_job_pid(client, auth, basic_service)
    _drain()
    r = client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    assert r.json() == {"status": "ok"}
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert len(queue) == 1
    assert queue[0]["source"] == "provider"
    assert queue[0]["provider_dispute_id"] == "dp_1"
    recorder = _drain()
    assert [m for m in recorder.sent if "ops@" in m[0]]


def test_chargeback_opened_without_payment_intent_is_noop(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A chargeback whose object carries only a charge id (no payment_intent)
    cannot be mapped to a job. It must not create a Dispute — it is recorded
    by dedup only and the webhook still returns its normal 2xx."""
    _job_id, _pid = _paid_job_pid(client, auth, basic_service)
    _drain()
    r = client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb_no_pi",
            "kind": "chargeback_opened",
            "object_id": "dp_no_pi",
            "amount_minor": 2000,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    with SessionLocal() as s:
        assert s.scalar(select(Dispute)) is None
        assert s.scalar(select(WebhookEvent)) is not None  # dedup row still recorded
    assert client.get("/v1/admin/disputes", headers=admin).json() == []
    recorder = _drain()
    assert recorder.sent == []  # unmappable event: no admin notification


def test_chargeback_lost_appends_loss_and_fee(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = _paid_job_pid(client, auth, basic_service)
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb2",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
            "outcome": "lost",
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert queue[0]["status"] == "chargeback_lost"
    with SessionLocal() as s:
        kinds = sorted((a.kind.value, str(a.amount)) for a in s.scalars(select(Adjustment)).all())
    assert kinds == [("chargeback_fee", "15.00"), ("chargeback_loss", "20.00")]
    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert summary["adjustments_net"] == "-35.00"


def test_chargeback_won_and_annotation_and_dedup(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id, pid = _paid_job_pid(client, auth, basic_service)
    _open_dispute(client, auth, job_id)  # buyer dispute already exists
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_9",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert len(queue) == 1  # annotated, not duplicated
    assert queue[0]["source"] == "buyer"
    assert queue[0]["provider_dispute_id"] == "dp_9"

    # Replay of the same event no-ops (webhook dedup).
    r = client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_9",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    assert r.json() == {"status": "duplicate"}

    # Won: status flips (dispute still open), no adjustments.
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb3",
            "kind": "chargeback_closed",
            "object_id": "dp_9",
            "related_id": pid,
            "outcome": "won",
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert queue[0]["status"] == "chargeback_won"
    with SessionLocal() as s:
        assert s.scalar(select(Adjustment)) is None


def test_chargeback_lost_after_resolution_keeps_status_appends_loss(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    job_id, pid = _paid_job_pid(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)
    _resolve(client, admin, str(dispute["id"]), "6.00", "0.00")
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb2",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
            "outcome": "lost",
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert queue[0]["status"] == "resolved"  # arbitration outcome preserved
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["chargeback_fee", "chargeback_loss", "refund"]  # double loss ledgered


def test_repeat_chargeback_readjudicates_status_but_preserves_resolved_rule(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _job_id, pid = _paid_job_pid(client, auth, basic_service)
    # First chargeback: lost.
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb2",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
            "outcome": "lost",
        },
    )
    # Second chargeback on the same job: won. Status must re-adjudicate.
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb3",
            "kind": "chargeback_opened",
            "object_id": "dp_2",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb4",
            "kind": "chargeback_closed",
            "object_id": "dp_2",
            "related_id": pid,
            "outcome": "won",
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert len(queue) == 1  # one-dispute-per-job: same row, re-annotated
    assert queue[0]["provider_dispute_id"] == "dp_2"
    assert queue[0]["status"] == "chargeback_won"  # latest provider outcome wins
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["chargeback_fee", "chargeback_loss"]  # only the LOST one ledgered


def test_chargeback_lost_with_no_amount_books_only_the_fee(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Folded minor: a zero/absent amount_minor must not book a $0.00 loss
    row alongside the fee — only the fee is a real charge."""
    _job_id, pid = _paid_job_pid(client, auth, basic_service)
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb2",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "outcome": "lost",
            # amount_minor omitted -> 0
        },
    )
    with SessionLocal() as s:
        kinds = sorted((a.kind.value, str(a.amount)) for a in s.scalars(select(Adjustment)).all())
    assert kinds == [("chargeback_fee", "15.00")]


def test_buyer_get_dispute_happy_path(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _open_dispute(client, auth, job_id)
    r = client.get(f"/v1/jobs/{job_id}/dispute", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == "open"
    assert body["reason"] == "work was not as described"
    assert body["refund_amount"] is None
    assert "clawback_amount" not in body  # buyer never sees seller money


def test_buyer_get_dispute_missing_is_404(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    r = client.get(f"/v1/jobs/{job_id}/dispute", headers=auth("buyer", "alice"))
    assert r.status_code == 404


def test_seller_get_dispute_on_someone_elses_job_is_404(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _open_dispute(client, auth, job_id)
    onboard_and_avail(client, auth, basic_service, "s2")
    r = client.get(f"/v1/seller/jobs/{job_id}/dispute", headers=auth("seller", "s2"))
    assert r.status_code == 404


def test_clawback_requires_a_paid_payout(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """I1: a FAILED payout (fully reversed transfer, id still set) has no
    money for the seller to have kept — clawing back against it must 409
    instead of booking a lying CLAWBACK row (fake) or 502ing forever (real
    Stripe)."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    fake_payments.next_transfer_status = PayoutStatus.FAILED  # created, then reversed
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    job_id = str(job["id"])
    r = client.post(
        f"/v1/jobs/{job_id}/dispute",
        json={"reason": "seller never delivered"},
        headers=auth("buyer", "alice"),
    )
    assert r.status_code == 201, r.text
    dispute = r.json()

    r = client.post(
        f"/v1/admin/disputes/{dispute['id']}/resolve",
        json={"refund_amount": "0.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 409


def test_resolve_pins_amounts_across_a_502_retry(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """C2: once a provider leg may have executed, a retry must converge on
    the SAME amounts — a mismatched retry is a 409, not a silent divergence
    between the books and what the provider already did."""
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)
    dispute_id = str(dispute["id"])

    # Refund succeeds, then the reversal leg fails -> 502.
    fake_payments.fail_keys = {f"reversal:{job_id}:dispute"}
    r = client.post(
        f"/v1/admin/disputes/{dispute_id}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 502

    # The pin survived the rollback: still open, but the amounts are visible.
    view = next(d for d in client.get("/v1/admin/disputes", headers=admin).json())
    assert view["status"] == "open"
    assert view["refund_amount"] == "6.00"
    assert view["clawback_amount"] == "4.00"

    # A retry with DIFFERENT amounts can't silently diverge from the
    # already-executed refund leg -> 409, not a mystery second refund.
    r = client.post(
        f"/v1/admin/disputes/{dispute_id}/resolve",
        json={"refund_amount": "0.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 409

    # Retry with the ORIGINAL amounts converges.
    resolved = _resolve(client, admin, dispute_id, "6.00", "4.00")
    assert resolved["status"] == "resolved"
    assert fake_payments.refund_keys == [
        f"refund:{job_id}:dispute",
        f"refund:{job_id}:dispute",
    ]  # two calls, identical key => provider-side replay, not a second refund
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["clawback", "refund"]  # exactly one adjustment per kind


def test_arbitration_after_lost_chargeback_claws_back_the_seller(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """I2: after a lost chargeback the dispute is chargeback_lost, not open —
    one-dispute-per-job means there is otherwise no way to ever claw back the
    at-fault seller. Arbitration must still be possible, and once it lands
    (RESOLVED) a later replayed chargeback_closed must not overwrite it."""
    _job_id, pid = _paid_job_pid(client, auth, basic_service)
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb1",
            "kind": "chargeback_opened",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
        },
    )
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb2",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "amount_minor": 2000,
            "outcome": "lost",
        },
    )
    dispute = client.get("/v1/admin/disputes", headers=admin).json()[0]
    assert dispute["status"] == "chargeback_lost"

    resolved = _resolve(client, admin, str(dispute["id"]), "0.00", "4.00")
    assert resolved["status"] == "resolved"
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["chargeback_fee", "chargeback_loss", "clawback"]

    # A later chargeback_closed on the same provider dispute (new event_id)
    # must not overwrite the arbitration outcome.
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_cb3",
            "kind": "chargeback_closed",
            "object_id": "dp_1",
            "related_id": pid,
            "outcome": "won",
        },
    )
    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert queue[0]["status"] == "resolved"


def test_concurrent_duplicate_resolve_cannot_double_book(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin's commit releases the dispute row lock for the provider-legs
    window; a duplicate resolve that wins the race during that window must
    make the loser 409 at its re-lock — not book a second set of adjustments
    for money the (idempotent-keyed) provider only moved once."""
    job_id = _completed_job(client, auth, basic_service)
    dispute = _open_dispute(client, auth, job_id)
    dispute_id = str(dispute["id"])
    real_reverse = fake_payments.reverse_transfer

    def winner_lands_mid_legs(
        provider_transfer_id: str, *, amount: Decimal, idempotency_key: str
    ) -> ReversalResult:
        result = real_reverse(provider_transfer_id, amount=amount, idempotency_key=idempotency_key)
        # The concurrent duplicate acquired the freed lock, replayed the same
        # provider legs, and fully resolved while this request was still in
        # its own legs.
        with SessionLocal() as s:
            d = s.get_one(Dispute, UUID(dispute_id))
            d.status = DisputeStatus.RESOLVED
            d.resolved_at = datetime.now(UTC)
            for kind, booked in (
                (AdjustmentKind.REFUND, "6.00"),
                (AdjustmentKind.CLAWBACK, "4.00"),
            ):
                s.add(
                    Adjustment(
                        job_id=UUID(job_id),
                        dispute_id=d.id,
                        kind=kind,
                        amount=Decimal(booked),
                        provider_ref="winner",
                    )
                )
            s.commit()
        return result

    monkeypatch.setattr(fake_payments, "reverse_transfer", winner_lands_mid_legs)
    r = client.post(
        f"/v1/admin/disputes/{dispute_id}/resolve",
        json={"refund_amount": "6.00", "clawback_amount": "4.00", "note": ""},
        headers=admin,
    )
    assert r.status_code == 409
    with SessionLocal() as s:
        kinds = sorted(a.kind.value for a in s.scalars(select(Adjustment)).all())
    assert kinds == ["clawback", "refund"]  # the winner's pair only, never four


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_dispute_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """UNIQUE(disputes.job_id) backstops the duplicate check; the loser must
    get the sequential path's 409, not a 500."""
    job_id = _completed_job(client, auth, basic_service)
    buyer = auth("buyer", "alice")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(
            f"/v1/jobs/{job_id}/dispute", json={"reason": "raced"}, headers=buyer
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [201, 409], codes

    with SessionLocal() as s:
        assert len(s.scalars(select(Dispute).where(Dispute.job_id == UUID(job_id))).all()) == 1
