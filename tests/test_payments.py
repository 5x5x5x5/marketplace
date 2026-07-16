"""Payment flows against the fake provider: onboarding, gating."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Job, Payment, SellerProfile, Transaction
from marketplace.models import JobStatus, PaymentStatus, PayoutStatus
from marketplace.payments import fake_provider
from marketplace.payments.fake import FakeProvider
from tests.conftest import IS_POSTGRES, AuthFactory, Header


def onboard_and_avail(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    """The standard seller setup: payment onboarding, then availability."""
    client.post("/v1/seller/payments/onboard", headers=auth("seller", seller))
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )


def new_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str) -> dict[str, object]:
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    return client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer)).json()


def test_onboard_returns_link_and_ready(client: TestClient, auth: AuthFactory) -> None:
    r = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    assert r.status_code == 200
    body = r.json()
    assert body["payments_ready"] is True  # fake is instantly ready
    assert body["onboarding_url"].startswith("https://fake.example/onboard/")


def test_onboard_is_idempotent(client: TestClient, auth: AuthFactory) -> None:
    first = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1")).json()
    second = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1")).json()
    assert first == second


def test_onboard_provider_outage_is_502_then_recovers(
    client: TestClient, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    fake_payments.fail_next_call = True
    r = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    assert r.status_code == 502  # provider outage, not a 500
    r2 = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    assert r2.status_code == 200
    assert r2.json()["payments_ready"] is True


def test_unonboarded_seller_never_matched(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    # Availability WITHOUT onboarding: the seller can't be paid, so can't be offered work.
    client.post(
        "/v1/seller/availability",
        json={"service_type_id": basic_service},
        headers=auth("seller", "ghost"),
    )
    job = new_job(client, auth, basic_service, "alice")
    assert job["status"] == "expired"  # no eligible seller
    assert client.get("/v1/seller/offers", headers=auth("seller", "ghost")).json() == []


def test_onboarded_seller_is_matched(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    assert job["status"] == "pending"
    assert len(client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()) == 1


def accept_first_offer(client: TestClient, seller: Header) -> dict[str, object]:
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]
    r = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r.status_code == 200, r.text
    return r.json()


def test_accept_charges_and_goes_accepted_inline(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """Fake charges succeed instantly → the job lands straight in ACCEPTED."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accepted = accept_first_offer(client, auth("seller", "s1"))
    assert accepted["status"] == "accepted"

    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "succeeded"
    assert view["client_secret"] is None  # nothing left for the buyer to confirm


def test_accept_with_async_charge_awaits_payment(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    """A pending charge parks the job in AWAITING_PAYMENT with a client_secret."""
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accepted = accept_first_offer(client, auth("seller", "s1"))
    assert accepted["status"] == "awaiting_payment"

    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "awaiting_payment"
    assert view["payment_status"] == "pending"
    assert str(view["client_secret"]).startswith("cs_fake_")


def test_awaiting_payment_holds_the_capacity_slot(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")  # capacity 1
    new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # awaiting payment
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "expired"  # only seller's slot is held


def test_provider_outage_rolls_accept_back(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    seller = auth("seller", "s1")
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]

    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r.status_code == 502

    # Nothing stuck: the offer is still open and a retry succeeds.
    assert client.get("/v1/seller/offers", headers=seller).json()[0]["id"] == offer["id"]
    r2 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r2.status_code == 200
    assert r2.json()["status"] == "accepted"


def pending_accept(
    client: TestClient, auth: AuthFactory, sid: str, fake: FakeProvider
) -> tuple[str, str]:
    """Set up a job accepted with a pending charge; return (job_id, provider_payment_id)."""
    fake.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )
    assert pid is not None
    return str(job["id"]), pid


def test_webhook_success_activates_the_job(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_1", "kind": "payment_succeeded", "object_id": pid},
    )
    assert r.json() == {"status": "ok"}
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "accepted"
    assert view["payment_status"] == "succeeded"


def test_webhook_dedup_is_a_noop(client: TestClient, auth: AuthFactory) -> None:
    """A replayed event is not re-applied — state changed since the first
    delivery must survive the replay untouched."""
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    event = {
        "event_id": "evt_dup",
        "kind": "account_updated",
        "object_id": "acct_fake_s1",
        "payments_ready": False,
    }
    assert client.post("/v1/payments/webhook", json=event).json() == {"status": "ok"}
    # Flip the state the event had set; a re-applied duplicate would flip it back.
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.payments_ready is False
        prof.payments_ready = True
        s.commit()
    assert client.post("/v1/payments/webhook", json=event).json() == {"status": "duplicate"}
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.payments_ready is True  # replay did NOT re-apply


def test_late_failure_never_undoes_success(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_s", "kind": "payment_succeeded", "object_id": pid},
    )
    client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_late_f", "kind": "payment_failed", "object_id": pid},
    )
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "accepted"
    assert view["payment_status"] == "succeeded"


def test_webhook_failure_records_but_keeps_waiting(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_f", "kind": "payment_failed", "object_id": pid},
    )
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "awaiting_payment"  # buyer can still retry confirmation
    assert view["payment_status"] == "failed"


def test_webhook_account_updated_flips_readiness(client: TestClient, auth: AuthFactory) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_a",
            "kind": "account_updated",
            "object_id": "acct_fake_s1",
            "payments_ready": False,
        },
    )

    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.payments_ready is False


def test_malformed_webhook_is_400(client: TestClient) -> None:
    r = client.post(
        "/v1/payments/webhook", content=b"not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400


def test_payment_timeout_expires_job_and_frees_slot(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    # Age the job past the payment TTL (white-box, same precedent as seed_rating).
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        assert job is not None
        job.accepted_at = datetime.now(UTC) - timedelta(minutes=999)
        s.commit()

    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()  # sweep on read
    assert view["status"] == "expired"
    assert fake_payments.cancelled == [pid]
    # The seller's slot is free again.
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "pending"


def test_sweep_void_failure_leaves_job_for_next_sweep(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    """A failed void must not expire the job — the next sweep retries the void."""
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        assert job is not None
        job.accepted_at = datetime.now(UTC) - timedelta(minutes=999)
        s.commit()

    fake_payments.fail_next_call = True
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()  # sweep on read
    assert view["status"] == "awaiting_payment"  # void failed → left for the next sweep
    assert fake_payments.cancelled == []

    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()  # next sweep
    assert view["status"] == "expired"
    assert fake_payments.cancelled == [pid]


def _completed_job(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200, r.text
    return str(job["id"])


def test_complete_transfers_the_payout(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    payouts = client.get("/v1/admin/payouts", headers=admin).json()
    assert len(payouts) == 1
    assert payouts[0]["job_id"] == job_id
    assert payouts[0]["status"] == "paid"
    assert str(payouts[0]["provider_transfer_id"]).startswith("tr_fake_")


def test_transfer_outage_marks_payout_failed_but_completes(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200  # the work happened; money owed is recorded, not dropped

    failed = client.get("/v1/admin/payouts", params={"status": "failed"}, headers=admin).json()
    assert len(failed) == 1 and failed[0]["provider_transfer_id"] is None


def test_admin_retries_failed_payout(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    fake_payments.fail_next_call = True
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    payout_id = client.get("/v1/admin/payouts", headers=admin).json()[0]["id"]

    r = client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    # No transfer was ever created (plain outage) → the retry replays the
    # ORIGINAL idempotency key, so it can never double-pay.
    assert fake_payments.transfer_keys == [f"transfer:{job['id']}"] * 2
    # Retrying a paid payout is a 409, not a double transfer.
    assert client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin).status_code == 409


def test_reversed_transfer_retry_forces_a_new_transfer(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """A transfer that was created then reversed must NOT be replayed via the
    original key — that returns the same reversed transfer and records PAID
    with no money moved. The retry must use a fresh key."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    fake_payments.next_transfer_status = PayoutStatus.FAILED  # created, then reversed
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    payout = client.get("/v1/admin/payouts", headers=admin).json()[0]
    assert payout["status"] == "failed"
    assert payout["provider_transfer_id"] is not None  # the reversed transfer exists

    r = client.post(f"/v1/admin/payouts/{payout['id']}/retry", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    assert len(fake_payments.transfer_keys) == 2
    assert ":retry:" in fake_payments.transfer_keys[1]
    assert fake_payments.transfer_keys[1] != fake_payments.transfer_keys[0]


def test_buyer_cancels_awaiting_payment_voids_charge(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert fake_payments.cancelled == [pid]
    assert fake_payments.refunded == []
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "failed"  # voided charge is recorded, not left pending


def test_admin_cancel_of_paid_job_refunds(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # instant success → paid

    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )

    r = client.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin)
    assert r.status_code == 200
    assert fake_payments.refunded == [pid]
    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "refunded"
    # The seller's slot is freed.
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "pending"


def test_stale_success_never_resurrects_a_refund(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """REFUNDED is terminal in the cash record: a late payment_succeeded (new
    event id, so it passes dedup) must not flip the payment back to SUCCEEDED."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # instant success → paid

    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )
    assert pid is not None

    r = client.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin)
    assert r.status_code == 200
    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "refunded"

    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_stale_success", "kind": "payment_succeeded", "object_id": pid},
    )
    assert r.json() == {"status": "ok"}  # recorded for dedup, applied as a no-op
    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "refunded"
    assert view["status"] == "cancelled"


def test_buyer_still_cannot_cancel_accepted(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 409  # paid + committed: only an admin unwinds this


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_cancel_vs_webhook_race(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Admin cancel of an AWAITING_PAYMENT job races the payment_succeeded
    webhook. Exactly one side wins; the pair (job.status, payment.status) must
    land consistent: (cancelled, FAILED) — cancel won, charge voided;
    (accepted, SUCCEEDED) — webhook won, cancel lost/409d; or
    (cancelled, REFUNDED) — webhook won, then cancel refunded the paid charge.
    NEVER (accepted, FAILED) or (cancelled, SUCCEEDED)."""
    onboard_and_avail(client, auth, basic_service, "s1")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    with SessionLocal() as s:
        ppid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )

    barrier = threading.Barrier(2)

    def do_cancel() -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin).status_code

    def do_webhook() -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(
            "/v1/payments/webhook",
            json={"event_id": "evt-race-1", "kind": "payment_succeeded", "object_id": ppid},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(do_cancel), pool.submit(do_webhook)
        codes = (f1.result(), f2.result())
    assert all(code in (200, 409) for code in codes), codes

    with SessionLocal() as s:
        final_job = s.get(Job, UUID(str(job["id"])))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(str(job["id"]))))
        assert final_job is not None and payment is not None
        pair = (final_job.status, payment.status)
    assert pair in (
        (JobStatus.CANCELLED, PaymentStatus.FAILED),
        (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED),
        (JobStatus.CANCELLED, PaymentStatus.REFUNDED),
    ), pair


def test_late_success_never_resurrects_a_voided_cancel(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Admin-cancel of an AWAITING_PAYMENT job voids the pending charge:
    payment -> FAILED, job -> CANCELLED. A late payment_succeeded for that
    same provider_payment_id (a delayed/replayed event, or the losing side
    of a cancel-vs-webhook race) must never resurrect the payment — the
    forbidden pair is (CANCELLED, SUCCEEDED): buyer charged for a cancelled
    job, no refund booked."""
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)

    r = client.post(f"/v1/admin/jobs/{job_id}/cancel", headers=admin)
    assert r.status_code == 200
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        assert job.status == JobStatus.CANCELLED
        assert payment.status == PaymentStatus.FAILED

    with caplog.at_level(logging.WARNING, logger="marketplace"):
        r = client.post(
            "/v1/payments/webhook",
            json={
                "event_id": "evt_late_success_voided",
                "kind": "payment_succeeded",
                "object_id": pid,
            },
        )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}  # event consumed/deduped; state change refused

    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        assert job.status == JobStatus.CANCELLED  # NOT resurrected
        assert payment.status == PaymentStatus.FAILED  # NOT resurrected

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "voided payment" in warnings[0].getMessage()


def test_decline_then_retry_on_same_pi_recovers(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
) -> None:
    """The legitimate recovery path: a card decline fails the pending charge
    but leaves the job AWAITING_PAYMENT (buyer can retry confirmation on the
    SAME PaymentIntent). A later payment_succeeded for that same
    provider_payment_id must be applied — the resurrection guard's
    job-status check exists precisely so this recovery is not mistaken for
    a voided-cancel/sweep resurrection."""
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)

    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_decline", "kind": "payment_failed", "object_id": pid},
    )
    assert r.status_code == 200
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        assert payment.status == PaymentStatus.FAILED
        assert job.status == JobStatus.AWAITING_PAYMENT  # buyer can still retry confirmation

    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_retry_success", "kind": "payment_succeeded", "object_id": pid},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        assert payment.status == PaymentStatus.SUCCEEDED
        assert job.status == JobStatus.ACCEPTED


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_cancel_vs_complete_race(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Admin cancel races seller complete on an ACCEPTED job (tryout finding
    F5). The loser must 409: exactly one side wins, and the money must match
    the winner — never a refunded buyer AND a booked transaction/paid payout.
    The bug class is the stale-relock trap: cancel's unlocked existence peek
    caches the Job, so its post-lock status guard read pre-race state."""
    onboard_and_avail(client, auth, basic_service, "s1")
    seller = auth("seller", "s1")
    codes: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def do_cancel(jid: str) -> None:
        barrier.wait()
        codes["cancel"] = (
            TestClient(api.app).post(f"/v1/admin/jobs/{jid}/cancel", headers=admin).status_code
        )

    def do_complete(jid: str) -> None:
        barrier.wait()
        codes["complete"] = (
            TestClient(api.app).post(f"/v1/seller/jobs/{jid}/complete", headers=seller).status_code
        )

    for attempt in range(5):
        job_id = str(new_job(client, auth, basic_service, "alice")["id"])
        offer = client.get("/v1/seller/offers", headers=seller).json()[0]
        client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)

        codes.clear()
        barrier.reset()
        threads = [
            threading.Thread(target=do_cancel, args=(job_id,)),
            threading.Thread(target=do_complete, args=(job_id,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [k for k, v in codes.items() if v == 200]
        assert len(wins) == 1, f"attempt {attempt}: both sides returned {codes}"
        with SessionLocal() as s:
            final_job = s.get(Job, UUID(job_id))
            payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
            tx_count = s.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.job_id == UUID(job_id))
            )
            assert final_job is not None and payment is not None
            if wins == ["complete"]:
                assert final_job.status == JobStatus.COMPLETED
                assert payment.status == PaymentStatus.SUCCEEDED
                assert tx_count == 1
            else:
                assert final_job.status == JobStatus.CANCELLED
                assert payment.status == PaymentStatus.REFUNDED
                assert tx_count == 0, "cancelled job must not keep a booked transaction"
