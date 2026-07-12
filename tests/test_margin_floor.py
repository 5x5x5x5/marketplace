"""Margin floor: a quote with sub-floor margin gets bumped or rejected."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str = "s1") -> None:
    r = client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )
    assert r.status_code == 200


def _quote_price(client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice") -> str:
    r = client.post("/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer))
    assert r.status_code == 200, r.json()
    return r.json()["buyer_price"]


def test_no_floor_uses_base_price(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service)
    assert _quote_price(client, basic_service, auth) == "20.00"


def test_floor_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    # base spread 20-14=6; floor 10 → buyer_price bumped to 14+10 = 24.
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "24.00"


def test_floor_pct_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    # pct 0.5 of base 20 = 10 → target 14+10 = 24.
    client.put("/v1/admin/config/margin_floor", json={"pct": 0.5}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "24.00"


def test_floor_above_ceiling_rejects(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    client.put(
        "/v1/admin/config/margin_floor",
        json={"absolute": 100, "ceiling_multiplier": 1.5},
        headers=admin,
    )
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "a")
    )
    assert r.status_code == 422
    assert "ceiling" in r.json()["detail"]  # generic message, no numbers leaked


def test_no_supply_no_floor_probe(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    # No sellers → no probe → base price even with a floor set.
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "20.00"
