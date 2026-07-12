"""Runtime-config tests: PUT /v1/admin/... changes behavior on the next request."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _price(client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice") -> str:
    r = client.post("/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer))
    assert r.status_code == 200
    return r.json()["buyer_price"]


def test_changing_pipeline_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    assert _price(client, basic_service, auth) == "20.00"

    client.put(
        "/v1/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
        headers=admin,
    )
    client.put(
        f"/v1/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
        headers=admin,
    )
    assert _price(client, basic_service, auth, buyer="bob") == "30.00"  # 20 * 1.5


def test_changing_adjuster_params_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    client.put(
        f"/v1/admin/config/pipelines/{basic_service}",
        json={"buyer": ["time_of_day_multiplier"], "seller": []},
        headers=admin,
    )
    client.put(
        "/v1/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 1.5 for h in range(24)}},
        headers=admin,
    )
    assert _price(client, basic_service, auth, buyer="b1") == "30.00"

    client.put(
        "/v1/admin/config/adjuster_params/time_of_day_multiplier",
        json={"multipliers": {str(h): 2.0 for h in range(24)}},
        headers=admin,
    )
    assert _price(client, basic_service, auth, buyer="b2") == "40.00"


def test_changing_base_price_changes_next_quote(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    assert _price(client, basic_service, auth, "b1") == "20.00"
    client.put(
        f"/v1/admin/config/service_types/{basic_service}",
        json={"base_buyer_price": 50, "base_seller_payout": 30},
        headers=admin,
    )
    assert _price(client, basic_service, auth, "b2") == "50.00"


def test_get_config_reflects_mutations(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    client.put("/v1/admin/config/margin_floor", json={"absolute": 5, "pct": 0.1}, headers=admin)
    client.put("/v1/admin/config/matching_strategy", json={"strategy": "fifo"}, headers=admin)

    body = client.get("/v1/admin/config", headers=admin).json()
    assert body["matching_strategy"] == "fifo"
    assert body["margin_floor"]["absolute"] == "5.00"
    assert basic_service in body["service_types"]


def test_unknown_strategy_rejected(client: TestClient, admin: Header) -> None:
    r = client.put("/v1/admin/config/matching_strategy", json={"strategy": "banana"}, headers=admin)
    assert r.status_code == 422


def test_unknown_adjuster_param_path_rejected(client: TestClient, admin: Header) -> None:
    r = client.put("/v1/admin/config/adjuster_params/banana", json={"k": 1}, headers=admin)
    assert r.status_code == 404
