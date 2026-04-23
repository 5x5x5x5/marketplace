"""Information-asymmetry tests.

Buyer-role responses must NOT include `seller_payout`.
Seller-role responses must NOT include `buyer_price`.
Admin-role responses (via /admin/transactions) DO include both.
"""

from fastapi.testclient import TestClient


def _setup(client: TestClient, sid: str) -> tuple[str, str]:
    r = client.post("/availability", json={"seller_id": "s1", "service_type_id": sid})
    assert r.status_code == 200
    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": sid})
    assert r.status_code == 200
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id})
    assert r.status_code == 200
    job_id = r.json()["id"]
    return job_id, "s1"


def test_buyer_view_omits_seller_payout(client: TestClient, basic_service: str) -> None:
    job_id, _ = _setup(client, basic_service)
    r = client.get(f"/jobs/{job_id}?role=buyer")
    assert r.status_code == 200
    body = r.json()
    assert "seller_payout" not in body
    assert "buyer_price" in body  # buyer is allowed to see what they pay


def test_create_job_response_omits_seller_payout(client: TestClient, basic_service: str) -> None:
    """The POST /jobs response is the buyer view too."""
    r = client.post("/availability", json={"seller_id": "s1", "service_type_id": basic_service})
    assert r.status_code == 200
    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id})
    assert r.status_code == 200
    body = r.json()
    assert "seller_payout" not in body


def test_seller_offered_view_omits_buyer_price(client: TestClient, basic_service: str) -> None:
    _setup(client, basic_service)
    r = client.get("/jobs/offered", params={"seller_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    offered = body[0]
    assert "buyer_price" not in offered
    assert "seller_payout" in offered  # the seller is allowed to see what they earn


def test_admin_transaction_includes_both(client: TestClient, basic_service: str) -> None:
    job_id, seller_id = _setup(client, basic_service)
    r = client.post(f"/jobs/{job_id}/accept", json={"seller_id": seller_id})
    assert r.status_code == 200
    r = client.post(f"/jobs/{job_id}/complete", json={"seller_id": seller_id})
    assert r.status_code == 200

    r = client.get("/admin/transactions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    tx = body[0]
    assert "buyer_price" in tx
    assert "seller_payout" in tx
    assert "margin" in tx
    assert tx["margin"] == tx["buyer_price"] - tx["seller_payout"]
