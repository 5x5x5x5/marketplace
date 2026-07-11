"""Margin floor: a quote with sub-floor margin gets bumped or rejected."""

from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import AuthFactory, Header


def _make_seller(client: TestClient, sid: str, auth: AuthFactory, seller_id: str = "s1") -> None:
    r = client.post(
        "/availability", json={"service_type_id": sid}, headers=auth("seller", seller_id)
    )
    assert r.status_code == 200


def _quote(client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice") -> Response:
    return client.post("/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer))


def test_no_floor_quote_uses_base_buyer_price(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _make_seller(client, basic_service, auth)
    r = _quote(client, basic_service, auth)
    assert r.status_code == 200
    # base_buyer_price is 20.0, no adjusters, no floor — quote is exactly 20.
    assert r.json()["buyer_price"] == 20.0


def test_floor_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _make_seller(client, basic_service, auth)
    # base spread = 20 - 14 = 6. Set floor to 10 absolute.
    r = client.put(
        "/admin/config/margin_floor",
        json={"absolute": 10.0, "ceiling_multiplier": 3.0},
        headers=admin,
    )
    assert r.status_code == 200

    r = _quote(client, basic_service, auth)
    assert r.status_code == 200
    # min_payout = 14, floor = 10 → buyer_price must be >= 24.
    assert r.json()["buyer_price"] == 24.0


def test_floor_pct_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _make_seller(client, basic_service, auth)
    # 50% pct floor: for base_bp=20, floor=10 (= 0.5*20), target = 14 + 10 = 24.
    r = client.put(
        "/admin/config/margin_floor",
        json={"pct": 0.5, "ceiling_multiplier": 3.0},
        headers=admin,
    )
    assert r.status_code == 200

    r = _quote(client, basic_service, auth)
    assert r.status_code == 200
    assert r.json()["buyer_price"] == 24.0


def test_floor_above_ceiling_rejects(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _make_seller(client, basic_service, auth)
    # floor 100 absolute, ceiling 1.5x (= 30) — floor-corrected price (114) blows past 30.
    r = client.put(
        "/admin/config/margin_floor",
        json={"absolute": 100.0, "ceiling_multiplier": 1.5},
        headers=admin,
    )
    assert r.status_code == 200
    r = _quote(client, basic_service, auth)
    assert r.status_code == 422
    # Message is generic (no numbers) so it can't leak the seller payout.
    assert "ceiling" in r.json()["detail"]


def test_no_supply_quote_succeeds_without_floor(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """With no sellers available, the quote uses the base buyer price (no probe)."""
    r = _quote(client, basic_service, auth)
    assert r.status_code == 200
    assert r.json()["buyer_price"] == 20.0


def test_floor_change_takes_effect_on_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _make_seller(client, basic_service, auth)

    r = _quote(client, basic_service, auth)
    assert r.json()["buyer_price"] == 20.0

    # Raise floor at runtime.
    r = client.put("/admin/config/margin_floor", json={"absolute": 8.0}, headers=admin)
    assert r.status_code == 200

    r = _quote(client, basic_service, auth, buyer="bob")
    # 14 + 8 = 22.
    assert r.json()["buyer_price"] == 22.0
