"""Matching strategy tests."""

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _seller(
    client: TestClient, auth: AuthFactory, sid: str, name: str, tier: str = "standard"
) -> None:
    # Assign tier (admin), then post availability (as the seller).
    r = client.put(f"/v1/admin/sellers/{name}", json={"tier": tier}, headers=auth("admin", "ops"))
    assert r.status_code == 200
    client.post("/v1/seller/payments/onboard", headers=auth("seller", name))
    r = client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", name)
    )
    assert r.status_code == 200


def _make_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str = "alice") -> None:
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    r = client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer))
    assert r.status_code == 200, r.json()


def _offered_to(client: TestClient, auth: AuthFactory, names: list[str]) -> str:
    for n in names:
        if client.get("/v1/seller/offers", headers=auth("seller", n)).json():
            return n
    return ""


def _tiered_pipeline(client: TestClient, admin: Header, sid: str, tiers: dict[str, float]) -> None:
    client.put(
        "/v1/admin/config/adjuster_params/seller_tier_multiplier",
        json={"tiers": tiers},
        headers=admin,
    )
    client.put(
        f"/v1/admin/config/pipelines/{sid}",
        json={"buyer": [], "seller": ["seller_tier_multiplier"]},
        headers=admin,
    )


def test_cheapest_payout_picks_lowest(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _tiered_pipeline(client, admin, basic_service, {"premium": 1.5, "standard": 1.0, "new": 0.7})
    _seller(client, auth, basic_service, "premium_pat", "premium")
    _seller(client, auth, basic_service, "standard_sam", "standard")
    _seller(client, auth, basic_service, "new_nat", "new")

    _make_job(client, auth, basic_service)
    # cheapest = 14 * 0.7 = 9.80 → new_nat.
    assert _offered_to(client, auth, ["premium_pat", "standard_sam", "new_nat"]) == "new_nat"


def test_fifo_picks_first_available(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    client.put("/v1/admin/config/matching_strategy", json={"strategy": "fifo"}, headers=admin)
    _seller(client, auth, basic_service, "first")
    _seller(client, auth, basic_service, "second")
    _seller(client, auth, basic_service, "third")

    _make_job(client, auth, basic_service)
    assert _offered_to(client, auth, ["first", "second", "third"]) == "first"


def test_highest_rated_picks_top(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    seed_rating: Callable[[str, int, int], None],
) -> None:
    client.put(
        "/v1/admin/config/matching_strategy", json={"strategy": "highest_rated"}, headers=admin
    )
    _seller(client, auth, basic_service, "low")
    _seller(client, auth, basic_service, "high")
    _seller(client, auth, basic_service, "mid")
    seed_rating("low", 35, 10)  # 3.5
    seed_rating("high", 49, 10)  # 4.9
    seed_rating("mid", 42, 10)  # 4.2

    _make_job(client, auth, basic_service)
    assert _offered_to(client, auth, ["low", "high", "mid"]) == "high"


def test_floor_filters_candidate(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A seller whose payout would violate the floor is skipped even if cheapest."""
    _tiered_pipeline(client, admin, basic_service, {"cheap": 1.4, "ok": 0.8})
    client.put("/v1/admin/config/margin_floor", json={"absolute": 1}, headers=admin)
    _seller(client, auth, basic_service, "cheap_seller", "cheap")  # payout 19.6, margin 0.4 < 1
    _seller(client, auth, basic_service, "ok_seller", "ok")  # payout 11.2, margin 8.8

    _make_job(client, auth, basic_service)
    assert _offered_to(client, auth, ["cheap_seller", "ok_seller"]) == "ok_seller"
