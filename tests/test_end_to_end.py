"""End-to-end test: quote → accept → seller match → complete → transaction."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def test_full_flow_books_correct_margin(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = auth("seller", "carol")
    buyer = auth("buyer", "alice")

    # Seller posts availability.
    r = client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    assert r.status_code == 200

    # Buyer requests a quote.
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 200
    quote = r.json()
    quote_id = quote["id"]
    assert quote["buyer_price"] == 20.0

    # Buyer accepts (creates a job, which is offered to a seller).
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    assert r.status_code == 200
    job_buyer_view = r.json()
    job_id = job_buyer_view["id"]
    assert job_buyer_view["seller_id"] == "carol"
    assert "seller_payout" not in job_buyer_view

    # Seller sees the offered job (without buyer_price).
    r = client.get("/jobs/offered", headers=seller)
    assert r.status_code == 200
    offered = r.json()
    assert len(offered) == 1
    assert offered[0]["id"] == job_id
    assert offered[0]["seller_payout"] == 14.0
    assert "buyer_price" not in offered[0]

    # Seller accepts.
    r = client.post(f"/jobs/{job_id}/accept", headers=seller)
    assert r.status_code == 200

    # Seller completes — transaction is booked.
    r = client.post(f"/jobs/{job_id}/complete", headers=seller)
    assert r.status_code == 200
    tx = r.json()
    assert tx["buyer_price"] == 20.0
    assert tx["seller_payout"] == 14.0
    assert tx["margin"] == 6.0
    assert tx["job_id"] == job_id

    # Admin ledger has it.
    r = client.get("/admin/transactions", headers=admin)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Admin summary reflects the take rate.
    r = client.get("/admin/margins/summary", headers=admin)
    summary = r.json()
    assert summary["transactions"] == 1.0
    assert summary["gross_revenue"] == 20.0
    assert summary["seller_payouts"] == 14.0
    assert summary["platform_margin"] == 6.0
    assert summary["take_rate"] == 0.3


def test_cannot_accept_others_offered_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    r = client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=buyer)
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    job_id = r.json()["id"]

    r = client.post(f"/jobs/{job_id}/accept", headers=auth("seller", "interloper"))
    assert r.status_code == 403


def test_cannot_complete_unmatched_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    r = client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=buyer)
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    job_id = r.json()["id"]

    # Skip the accept step, try to complete.
    r = client.post(f"/jobs/{job_id}/complete", headers=seller)
    assert r.status_code == 409


def test_quote_consumed_after_job_creation(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """A quote is single-use; trying to make a second job from it 404s."""
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    r = client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=buyer)
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    assert r.status_code == 200
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    assert r.status_code == 404
