"""Payment flows against the fake provider: onboarding, gating."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory


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
