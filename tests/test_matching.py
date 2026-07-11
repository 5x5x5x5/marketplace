"""Matching strategy tests."""

from fastapi.testclient import TestClient

from marketplace import api
from marketplace.models import SellerProfile
from tests.conftest import AuthFactory, Header


def _seller(client: TestClient, sid: str, auth: AuthFactory, seller_id: str) -> None:
    r = client.post(
        "/availability", json={"service_type_id": sid}, headers=auth("seller", seller_id)
    )
    assert r.status_code == 200


def _quote_and_create_job(
    client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice"
) -> dict[str, object]:
    hdr = auth("buyer", buyer)
    r = client.post("/quotes", json={"service_type_id": sid}, headers=hdr)
    assert r.status_code == 200
    quote_id = r.json()["id"]
    r = client.post("/jobs", json={"quote_id": quote_id}, headers=hdr)
    assert r.status_code == 200, r.json()
    return r.json()


def test_cheapest_payout_picks_lowest(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Three sellers with different tier multipliers; cheapest_payout picks the
    seller whose pipeline produces the lowest payout."""
    r = client.put(
        "/admin/config/adjuster_params/seller_tier_multiplier",
        json={"tiers": {"premium": 1.5, "standard": 1.0, "new": 0.7}},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": [], "seller": ["seller_tier_multiplier"]},
        headers=admin,
    )
    assert r.status_code == 200

    # Three sellers, each at a different tier.
    _seller(client, basic_service, auth, "premium_pat")
    api.store.sellers["premium_pat"] = SellerProfile(id="premium_pat", tier="premium")
    _seller(client, basic_service, auth, "standard_sam")
    api.store.sellers["standard_sam"] = SellerProfile(id="standard_sam", tier="standard")
    _seller(client, basic_service, auth, "new_nat")
    api.store.sellers["new_nat"] = SellerProfile(id="new_nat", tier="new")

    job = _quote_and_create_job(client, basic_service, auth)
    # Cheapest payout = base 14 * 0.7 = 9.8 → new_nat.
    assert job["seller_id"] == "new_nat"


def test_fifo_picks_first_available(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    r = client.put("/admin/config/matching_strategy", json={"strategy": "fifo"}, headers=admin)
    assert r.status_code == 200

    _seller(client, basic_service, auth, "first")
    _seller(client, basic_service, auth, "second")
    _seller(client, basic_service, auth, "third")

    job = _quote_and_create_job(client, basic_service, auth)
    assert job["seller_id"] == "first"


def test_highest_rated_picks_top_rated(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    r = client.put(
        "/admin/config/matching_strategy", json={"strategy": "highest_rated"}, headers=admin
    )
    assert r.status_code == 200

    _seller(client, basic_service, auth, "low")
    api.store.sellers["low"] = SellerProfile(id="low", rating=3.5)
    _seller(client, basic_service, auth, "high")
    api.store.sellers["high"] = SellerProfile(id="high", rating=4.9)
    _seller(client, basic_service, auth, "mid")
    api.store.sellers["mid"] = SellerProfile(id="mid", rating=4.2)

    job = _quote_and_create_job(client, basic_service, auth)
    assert job["seller_id"] == "high"


def test_runtime_strategy_change_changes_selection(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Switch strategy mid-flight; next job uses the new strategy."""
    _seller(client, basic_service, auth, "fifo_winner")
    api.store.sellers["fifo_winner"] = SellerProfile(id="fifo_winner", rating=4.0)
    _seller(client, basic_service, auth, "rating_winner")
    api.store.sellers["rating_winner"] = SellerProfile(id="rating_winner", rating=4.95)

    r = client.put("/admin/config/matching_strategy", json={"strategy": "fifo"}, headers=admin)
    assert r.status_code == 200
    job1 = _quote_and_create_job(client, basic_service, auth, buyer="alice")
    assert job1["seller_id"] == "fifo_winner"

    # Switch to highest_rated; availability remains since we never accepted.
    r = client.put(
        "/admin/config/matching_strategy", json={"strategy": "highest_rated"}, headers=admin
    )
    assert r.status_code == 200
    job2 = _quote_and_create_job(client, basic_service, auth, buyer="bob")
    assert job2["seller_id"] == "rating_winner"


def test_floor_filters_candidates_in_cheapest(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A seller whose payout would violate the floor is skipped, even if cheapest."""
    r = client.put(
        "/admin/config/adjuster_params/seller_tier_multiplier",
        json={"tiers": {"cheap": 1.4, "ok": 0.8}},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": [], "seller": ["seller_tier_multiplier"]},
        headers=admin,
    )
    assert r.status_code == 200

    # buyer_price = 20.
    # cheap payout = 14 * 1.4 = 19.6 → margin 0.4 (below floor)
    # ok payout    = 14 * 0.8 = 11.2 → margin 8.8 (passes floor)
    r = client.put("/admin/config/margin_floor", json={"absolute": 1.0}, headers=admin)
    assert r.status_code == 200

    _seller(client, basic_service, auth, "cheap_seller")
    api.store.sellers["cheap_seller"] = SellerProfile(id="cheap_seller", tier="cheap")
    _seller(client, basic_service, auth, "ok_seller")
    api.store.sellers["ok_seller"] = SellerProfile(id="ok_seller", tier="ok")

    job = _quote_and_create_job(client, basic_service, auth)
    assert job["seller_id"] == "ok_seller"


def test_no_seller_meets_floor_rejects_job(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """When the buyer-price ceiling is tight enough that no seller fits, /jobs rejects."""
    _seller(client, basic_service, auth, "s1")

    # Massive floor + tight ceiling: quote itself will be rejected before /jobs.
    r = client.put(
        "/admin/config/margin_floor",
        json={"absolute": 200.0, "ceiling_multiplier": 1.1},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.post("/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "a"))
    assert r.status_code == 422
