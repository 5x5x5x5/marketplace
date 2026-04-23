"""Runtime-config tests.

Changing the config via PUT /admin/... must change behavior on the next
request without any restart or import-time work.
"""

from fastapi.testclient import TestClient


def _quote_price(client: TestClient, sid: str, buyer: str = "alice") -> float:
    r = client.post("/quotes", json={"buyer_id": buyer, "service_type_id": sid})
    assert r.status_code == 200
    return float(r.json()["buyer_price"])


def test_changing_pipeline_changes_next_quote(client: TestClient, basic_service: str) -> None:
    r = client.post("/availability", json={"seller_id": "s1", "service_type_id": basic_service})
    assert r.status_code == 200

    base_price = _quote_price(client, basic_service)
    assert base_price == 20.0

    # Add a buyer-side adjuster that doubles when supply is zero.
    # We have one seller, so we'll use time_of_day_multiplier instead — deterministic
    # and not dependent on supply state.
    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
    )
    assert r.status_code == 200

    new_price = _quote_price(client, basic_service, buyer="bob")
    assert new_price == 30.0  # 20 * 1.5
    assert new_price != base_price


def test_changing_adjuster_params_changes_next_quote(
    client: TestClient, basic_service: str
) -> None:
    r = client.post("/availability", json={"seller_id": "s1", "service_type_id": basic_service})
    assert r.status_code == 200

    # Install pipeline once.
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
    )
    assert r.status_code == 200

    # First params: 1.5x
    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
    )
    assert r.status_code == 200
    assert _quote_price(client, basic_service, buyer="b1") == 30.0

    # Update params: 2.0x — same pipeline, different multiplier.
    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 2.0 for h in range(24)}},
    )
    assert r.status_code == 200
    assert _quote_price(client, basic_service, buyer="b2") == 40.0


def test_changing_service_type_base_price_changes_next_quote(
    client: TestClient, basic_service: str
) -> None:
    r = client.post("/availability", json={"seller_id": "s1", "service_type_id": basic_service})
    assert r.status_code == 200

    assert _quote_price(client, basic_service, "b1") == 20.0

    # Bump base price.
    r = client.put(
        f"/admin/config/service_types/{basic_service}",
        json={"base_buyer_price": 50.0, "base_seller_payout": 30.0},
    )
    assert r.status_code == 200

    assert _quote_price(client, basic_service, "b2") == 50.0


def test_get_config_reflects_mutations(client: TestClient, basic_service: str) -> None:
    r = client.put("/admin/config/margin_floor", json={"absolute": 5.0, "pct": 0.1})
    assert r.status_code == 200
    r = client.put("/admin/config/matching_strategy", json={"strategy": "fifo"})
    assert r.status_code == 200

    r = client.get("/admin/config")
    body = r.json()
    assert body["matching_strategy"] == "fifo"
    assert body["margin_floor"]["absolute"] == 5.0
    assert body["margin_floor"]["pct"] == 0.1
    assert basic_service in body["service_types"]


def test_unknown_strategy_rejected(client: TestClient, basic_service: str) -> None:
    del basic_service  # unused, but pulled in for the autouse fixture chain
    r = client.put("/admin/config/matching_strategy", json={"strategy": "banana"})
    assert r.status_code == 422


def test_unknown_adjuster_param_path_rejected(client: TestClient, basic_service: str) -> None:
    del basic_service
    r = client.put("/admin/config/adjuster_params/banana_adjuster", json={"k": 1})
    assert r.status_code == 404
