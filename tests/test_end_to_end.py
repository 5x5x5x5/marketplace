"""End-to-end: quote → offer → accept → complete → transaction → review."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", seller))
    r = client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )
    assert r.status_code == 200


def test_full_flow_books_correct_margin(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = auth("seller", "carol")
    buyer = auth("buyer", "alice")
    _available(client, auth, basic_service, "carol")

    r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 201
    quote = r.json()
    assert quote["buyer_price"] == "20.00"

    r = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert r.status_code == 201
    job = r.json()
    job_id = job["id"]
    assert job["status"] == "pending"
    assert "seller_payout" not in job

    r = client.get("/v1/seller/offers", headers=seller)
    assert r.status_code == 200
    offers = r.json()
    assert len(offers) == 1
    assert offers[0]["seller_payout"] == "14.00"
    assert "buyer_price" not in offers[0]
    offer_id = offers[0]["id"]

    r = client.post(f"/v1/seller/offers/{offer_id}/accept", headers=seller)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    r = client.post(f"/v1/seller/jobs/{job_id}/complete", headers=seller)
    assert r.status_code == 200
    receipt = r.json()
    # The completion receipt is the SELLER's view: their payout, never the
    # buyer price or the platform margin (the spread stays invisible).
    assert receipt["seller_payout"] == "14.00"
    assert set(receipt) == {"job_id", "seller_payout", "completed_at"}

    r = client.post(f"/v1/jobs/{job_id}/review", json={"rating": 5}, headers=buyer)
    assert r.status_code == 201

    r = client.get("/v1/admin/margins/summary", headers=admin)
    summary = r.json()
    assert summary["transactions"] == 1
    assert summary["gross_revenue"] == "20.00"
    assert summary["platform_margin"] == "6.00"
    assert summary["take_rate"] == 0.3


def test_cannot_accept_others_offer(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service, "s1")
    buyer = auth("buyer", "alice")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer)
    offer_id = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]["id"]

    r = client.post(f"/v1/seller/offers/{offer_id}/accept", headers=auth("seller", "interloper"))
    assert r.status_code == 404  # not this seller's offer


def test_cannot_complete_unaccepted_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    _available(client, auth, basic_service, "s1")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    job_id = client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).json()["id"]

    # Job is PENDING — no seller is assigned yet, so completing it is forbidden.
    r = client.post(f"/v1/seller/jobs/{job_id}/complete", headers=seller)
    assert r.status_code == 403


def test_quote_is_single_use(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    _available(client, auth, basic_service, "s1")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    assert client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).status_code == 201
    assert client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).status_code == 404
