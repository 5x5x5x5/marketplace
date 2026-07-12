"""Payment flows against the fake provider: onboarding, gating."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Payment, SellerProfile
from marketplace.models import PaymentStatus
from marketplace.payments.fake import FakeProvider
from tests.conftest import AuthFactory, Header


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


def _accept_first_offer(client: TestClient, seller: Header) -> dict[str, object]:
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
    accepted = _accept_first_offer(client, auth("seller", "s1"))
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
    accepted = _accept_first_offer(client, auth("seller", "s1"))
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
    _accept_first_offer(client, auth("seller", "s1"))  # awaiting payment
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


def _pending_accept(
    client: TestClient, auth: AuthFactory, sid: str, fake: FakeProvider
) -> tuple[str, str]:
    """Set up a job accepted with a pending charge; return (job_id, provider_payment_id)."""
    fake.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )
    assert pid is not None
    return str(job["id"]), pid


def test_webhook_success_activates_the_job(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
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
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
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
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
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
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
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


def _completed_job(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
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
    _accept_first_offer(client, auth("seller", "s1"))
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
    _accept_first_offer(client, auth("seller", "s1"))
    fake_payments.fail_next_call = True
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    payout_id = client.get("/v1/admin/payouts", headers=admin).json()[0]["id"]

    r = client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    # Retrying a paid payout is a 409, not a double transfer.
    assert client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin).status_code == 409


def test_buyer_cancels_awaiting_payment_voids_charge(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert fake_payments.cancelled == [pid]
    assert fake_payments.refunded == []


def test_admin_cancel_of_paid_job_refunds(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))  # instant success → paid

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


def test_buyer_still_cannot_cancel_accepted(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 409  # paid + committed: only an admin unwinds this
