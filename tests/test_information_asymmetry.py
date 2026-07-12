"""Information asymmetry: buyers never see seller_payout, sellers never see buyer_price."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _offer(client: TestClient, sid: str, auth: AuthFactory) -> tuple[str, str]:
    """Set up one PENDING job with an open offer to seller 's1'. Returns (job_id, offer_id)."""
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    client.post("/v1/seller/payments/onboard", headers=seller)
    client.post("/v1/seller/availability", json={"service_type_id": sid}, headers=seller)
    qid = client.post("/v1/quotes", json={"service_type_id": sid}, headers=buyer).json()["id"]
    job_id = client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).json()["id"]
    offer_id = client.get("/v1/seller/offers", headers=seller).json()[0]["id"]
    return job_id, offer_id


def test_buyer_view_omits_seller_payout(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id, _ = _offer(client, basic_service, auth)
    body = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert "seller_payout" not in body
    assert body["buyer_price"] == "20.00"  # buyer sees what they pay


def test_seller_offer_omits_buyer_price(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _offer(client, basic_service, auth)
    offers = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()
    assert "buyer_price" not in offers[0]
    assert offers[0]["seller_payout"] == "14.00"  # seller sees what they earn


def test_seller_cannot_read_buyer_view(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id, _ = _offer(client, basic_service, auth)
    # A seller token on the buyer endpoint is rejected by role, not just by ownership.
    assert client.get(f"/v1/jobs/{job_id}", headers=auth("seller", "s1")).status_code == 403


def test_buyer_cannot_read_seller_offers(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _offer(client, basic_service, auth)
    assert client.get("/v1/seller/offers", headers=auth("buyer", "alice")).status_code == 403


def test_admin_transaction_includes_both(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = auth("seller", "s1")
    job_id, offer_id = _offer(client, basic_service, auth)
    client.post(f"/v1/seller/offers/{offer_id}/accept", headers=seller)
    client.post(f"/v1/seller/jobs/{job_id}/complete", headers=seller)

    tx = client.get("/v1/admin/transactions", headers=admin).json()[0]
    assert tx["buyer_price"] == "20.00"
    assert tx["seller_payout"] == "14.00"
    assert tx["margin"] == "6.00"
