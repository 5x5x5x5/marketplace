"""Runtime-config tests.

Changing the config via PUT /admin/... must change behavior on the next
request without any restart or import-time work.
"""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _quote_price(client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice") -> float:
    r = client.post("/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer))
    assert r.status_code == 200
    return float(r.json()["buyer_price"])


def test_changing_pipeline_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    r = client.post(
        "/availability", json={"service_type_id": basic_service}, headers=auth("seller", "s1")
    )
    assert r.status_code == 200

    base_price = _quote_price(client, basic_service, auth)
    assert base_price == 20.0

    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
        headers=admin,
    )
    assert r.status_code == 200

    new_price = _quote_price(client, basic_service, auth, buyer="bob")
    assert new_price == 30.0  # 20 * 1.5
    assert new_price != base_price


def test_changing_adjuster_params_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    r = client.post(
        "/availability", json={"service_type_id": basic_service}, headers=auth("seller", "s1")
    )
    assert r.status_code == 200

    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
        headers=admin,
    )
    assert r.status_code == 200

    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
        headers=admin,
    )
    assert r.status_code == 200
    assert _quote_price(client, basic_service, auth, buyer="b1") == 30.0

    r = client.put(
        "/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 2.0 for h in range(24)}},
        headers=admin,
    )
    assert r.status_code == 200
    assert _quote_price(client, basic_service, auth, buyer="b2") == 40.0


def test_changing_service_type_base_price_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    r = client.post(
        "/availability", json={"service_type_id": basic_service}, headers=auth("seller", "s1")
    )
    assert r.status_code == 200

    assert _quote_price(client, basic_service, auth, "b1") == 20.0

    r = client.put(
        f"/admin/config/service_types/{basic_service}",
        json={"base_buyer_price": 50.0, "base_seller_payout": 30.0},
        headers=admin,
    )
    assert r.status_code == 200

    assert _quote_price(client, basic_service, auth, "b2") == 50.0


def test_get_config_reflects_mutations(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    r = client.put("/admin/config/margin_floor", json={"absolute": 5.0, "pct": 0.1}, headers=admin)
    assert r.status_code == 200
    r = client.put("/admin/config/matching_strategy", json={"strategy": "fifo"}, headers=admin)
    assert r.status_code == 200

    r = client.get("/admin/config", headers=admin)
    body = r.json()
    assert body["matching_strategy"] == "fifo"
    assert body["margin_floor"]["absolute"] == 5.0
    assert body["margin_floor"]["pct"] == 0.1
    assert basic_service in body["service_types"]


def test_unknown_strategy_rejected(client: TestClient, basic_service: str, admin: Header) -> None:
    del basic_service  # unused, but pulled in for the autouse fixture chain
    r = client.put("/admin/config/matching_strategy", json={"strategy": "banana"}, headers=admin)
    assert r.status_code == 422


def test_unknown_adjuster_param_path_rejected(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    del basic_service
    r = client.put("/admin/config/adjuster_params/banana_adjuster", json={"k": 1}, headers=admin)
    assert r.status_code == 404
