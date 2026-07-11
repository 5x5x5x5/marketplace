"""Information-asymmetry tests.

Buyer-role responses must NOT include `seller_payout`.
Seller-role responses must NOT include `buyer_price`.
Admin-role responses (via /admin/transactions) DO include both.
"""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _setup(client: TestClient, sid: str, auth: AuthFactory) -> str:
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    r = client.post("/availability", json={"service_type_id": sid}, headers=seller)
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": sid}, headers=buyer)
    assert r.status_code == 200
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    assert r.status_code == 200
    return r.json()["id"]


def test_buyer_view_omits_seller_payout(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _setup(client, basic_service, auth)
    r = client.get(f"/jobs/{job_id}", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    body = r.json()
    assert "seller_payout" not in body
    assert "buyer_price" in body  # buyer is allowed to see what they pay


def test_create_job_response_omits_seller_payout(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """The POST /jobs response is the buyer view too."""
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    r = client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=buyer)
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)
    assert r.status_code == 200
    body = r.json()
    assert "seller_payout" not in body


def test_seller_offered_view_omits_buyer_price(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _setup(client, basic_service, auth)
    r = client.get("/jobs/offered", headers=auth("seller", "s1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    offered = body[0]
    assert "buyer_price" not in offered
    assert "seller_payout" in offered  # the seller is allowed to see what they earn


def test_admin_transaction_includes_both(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _setup(client, basic_service, auth)
    seller = auth("seller", "s1")
    r = client.post(f"/jobs/{job_id}/accept", headers=seller)
    assert r.status_code == 200
    r = client.post(f"/jobs/{job_id}/complete", headers=seller)
    assert r.status_code == 200

    r = client.get("/admin/transactions", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    tx = body[0]
    assert "buyer_price" in tx
    assert "seller_payout" in tx
    assert "margin" in tx
    assert tx["margin"] == tx["buyer_price"] - tx["seller_payout"]
