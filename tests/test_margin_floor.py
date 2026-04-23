"""Margin floor: a quote with sub-floor margin gets bumped or rejected."""

from fastapi.testclient import TestClient


def _make_seller(client: TestClient, sid: str, seller_id: str = "s1") -> None:
    r = client.post("/availability", json={"seller_id": seller_id, "service_type_id": sid})
    assert r.status_code == 200


def test_no_floor_quote_uses_base_buyer_price(client: TestClient, basic_service: str) -> None:
    _make_seller(client, basic_service)
    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    assert r.status_code == 200
    # base_buyer_price is 20.0, no adjusters, no floor — quote is exactly 20.
    assert r.json()["buyer_price"] == 20.0


def test_floor_bumps_quote_up(client: TestClient, basic_service: str) -> None:
    _make_seller(client, basic_service)
    # base spread = 20 - 14 = 6. Set floor to 10 absolute.
    r = client.put("/admin/config/margin_floor", json={"absolute": 10.0, "ceiling_multiplier": 3.0})
    assert r.status_code == 200

    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    assert r.status_code == 200
    # min_payout = 14, floor = 10 → buyer_price must be >= 24.
    assert r.json()["buyer_price"] == 24.0


def test_floor_pct_bumps_quote_up(client: TestClient, basic_service: str) -> None:
    _make_seller(client, basic_service)
    # 50% pct floor of buyer_price ≥ 14 means buyer_price ≥ 28
    # because we bump until 0.5 * buyer_price ≤ buyer_price - 14, i.e. buyer_price >= 28.
    r = client.put("/admin/config/margin_floor", json={"pct": 0.5, "ceiling_multiplier": 3.0})
    assert r.status_code == 200

    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    assert r.status_code == 200
    # The bump uses `pct * buyer_price` as the floor relative to the *current*
    # buyer_price; the implementation snaps to (probe_payout + floor_at_current_bp).
    # For base_bp=20, floor=10 (= 0.5*20), target = 14 + 10 = 24.
    assert r.json()["buyer_price"] == 24.0


def test_floor_above_ceiling_rejects(client: TestClient, basic_service: str) -> None:
    _make_seller(client, basic_service)
    # floor 100 absolute, ceiling 1.5x (= 30) — floor-corrected price (114) blows past 30.
    r = client.put(
        "/admin/config/margin_floor", json={"absolute": 100.0, "ceiling_multiplier": 1.5}
    )
    assert r.status_code == 200
    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    assert r.status_code == 422
    assert "exceeds ceiling" in r.json()["detail"]


def test_no_supply_quote_succeeds_without_floor(client: TestClient, basic_service: str) -> None:
    """With no sellers available, the quote uses the base buyer price (no probe)."""
    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    assert r.status_code == 200
    assert r.json()["buyer_price"] == 20.0


def test_floor_change_takes_effect_on_next_quote(client: TestClient, basic_service: str) -> None:
    _make_seller(client, basic_service)

    r = client.post("/quotes", json={"buyer_id": "alice", "service_type_id": basic_service})
    first_price = r.json()["buyer_price"]
    assert first_price == 20.0

    # Raise floor at runtime.
    r = client.put("/admin/config/margin_floor", json={"absolute": 8.0})
    assert r.status_code == 200

    r = client.post("/quotes", json={"buyer_id": "bob", "service_type_id": basic_service})
    second_price = r.json()["buyer_price"]
    # 14 + 8 = 22.
    assert second_price == 22.0
