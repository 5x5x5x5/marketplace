"""Auth gates, capacity enforcement, admin-input validation, token expiry."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from marketplace.auth import mint_token
from marketplace.models import MarginFloorBody, ServiceTypeBody
from tests.conftest import AuthFactory, Header


def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )


def _new_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str) -> str:
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    return client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer)).json()[
        "id"
    ]


# ---------- Auth ----------


def test_admin_requires_token(client: TestClient) -> None:
    assert client.get("/v1/admin/transactions").status_code == 401
    assert (
        client.get(
            "/v1/admin/transactions", headers={"Authorization": "Bearer garbage"}
        ).status_code
        == 401
    )


def test_admin_rejects_non_admin(client: TestClient, auth: AuthFactory) -> None:
    assert client.get("/v1/admin/transactions", headers=auth("buyer", "alice")).status_code == 403


def test_expired_token_rejected(client: TestClient) -> None:
    header = {"Authorization": f"Bearer {mint_token('buyer', 'alice', ttl_hours=-1)}"}
    assert (
        client.post("/v1/quotes", json={"service_type_id": "x"}, headers=header).status_code == 401
    )


# ---------- Capacity ----------


def test_capacity_blocks_second_offer(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A capacity-1 seller who has accepted a job is no longer offered new ones."""
    _available(client, auth, basic_service, "solo")  # capacity defaults to 1
    seller = auth("seller", "solo")

    _new_job(client, auth, basic_service, "alice")
    offer1 = client.get("/v1/seller/offers", headers=seller).json()[0]["id"]
    assert client.post(f"/v1/seller/offers/{offer1}/accept", headers=seller).status_code == 200

    # Second job: the only seller is now at capacity → no offer → job EXPIRED.
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "bob")
    )
    qid = r.json()["id"]
    job2 = client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", "bob")).json()
    assert job2["status"] == "expired"


def test_capacity_guard_on_concurrent_offers(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Two offers reach a capacity-1 seller before either is accepted; only one accept wins."""
    _available(client, auth, basic_service, "solo")
    seller = auth("seller", "solo")

    _new_job(client, auth, basic_service, "alice")
    _new_job(client, auth, basic_service, "bob")  # active jobs still 0, so also offered to solo
    offers = client.get("/v1/seller/offers", headers=seller).json()
    assert len(offers) == 2

    assert (
        client.post(f"/v1/seller/offers/{offers[0]['id']}/accept", headers=seller).status_code
        == 200
    )
    # Second accept exceeds capacity 1.
    r = client.post(f"/v1/seller/offers/{offers[1]['id']}/accept", headers=seller)
    assert r.status_code == 409


def test_higher_capacity_allows_two(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    client.put("/v1/admin/sellers/solo", json={"capacity": 2}, headers=admin)
    _available(client, auth, basic_service, "solo")
    seller = auth("seller", "solo")

    _new_job(client, auth, basic_service, "alice")
    _new_job(client, auth, basic_service, "bob")
    offers = client.get("/v1/seller/offers", headers=seller).json()
    assert (
        client.post(f"/v1/seller/offers/{offers[0]['id']}/accept", headers=seller).status_code
        == 200
    )
    assert (
        client.post(f"/v1/seller/offers/{offers[1]['id']}/accept", headers=seller).status_code
        == 200
    )


# ---------- Validation ----------


def test_unknown_adjuster_in_pipeline_rejected(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    r = client.put(
        f"/v1/admin/config/pipelines/{basic_service}",
        json={"buyer": ["does_not_exist"], "seller": []},
        headers=admin,
    )
    assert r.status_code == 422


def test_margin_floor_bounds(client: TestClient, admin: Header) -> None:
    for body in ({"absolute": -5}, {"pct": -0.1}, {"pct": 1.0}, {"ceiling_multiplier": 0}):
        assert (
            client.put("/v1/admin/config/margin_floor", json=body, headers=admin).status_code == 422
        )
    # NaN/inf can't be sent as JSON; the Decimal guard is asserted at the model layer.
    for bad in ("nan", "inf"):
        with pytest.raises(ValidationError):
            MarginFloorBody(absolute=Decimal(bad))


def test_service_type_bounds(client: TestClient, admin: Header) -> None:
    for body in (
        {"base_buyer_price": 0, "base_seller_payout": 10},
        {"base_buyer_price": 20, "base_seller_payout": -1},
    ):
        assert (
            client.put("/v1/admin/config/service_types/x", json=body, headers=admin).status_code
            == 422
        )
    with pytest.raises(ValidationError):
        ServiceTypeBody(base_buyer_price=Decimal("inf"), base_seller_payout=Decimal(10))


def test_healthz_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
