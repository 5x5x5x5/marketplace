"""Payment flows against the fake provider: onboarding, gating."""

from fastapi.testclient import TestClient

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
