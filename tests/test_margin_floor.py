"""Margin floor: a quote with sub-floor margin gets bumped or rejected."""

from decimal import Decimal

from fastapi.testclient import TestClient

from marketplace.config import FeeConfig
from marketplace.matching import estimated_fee
from tests.conftest import AuthFactory, Header


def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str = "s1") -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", seller))
    r = client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )
    assert r.status_code == 200


def _quote_price(client: TestClient, sid: str, auth: AuthFactory, buyer: str = "alice") -> str:
    r = client.post("/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer))
    assert r.status_code == 201, r.json()
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
    # base spread 20-14=6; floor 10 + fee(bp) → first ceil lands on 25, which
    # STILL undershoots (25-14=11.00 < 10+fee(25)=11.03) — the verify loop
    # bumps once more. 26-14=12.00 >= 10+fee(26)=11.05.
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "26.00"


def test_floor_pct_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    # pct 0.5: required grows ~0.529 per bumped unit — the loop walks from the
    # first ceil (25) to the fixed point: 31-14=17.00 >= 15.50+fee(31)=16.70.
    client.put("/v1/admin/config/margin_floor", json={"pct": 0.5}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "31.00"


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


def test_zero_fees_restore_gross_floor(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Pinning fees to zero recovers the pre-fee bump math exactly."""
    _available(client, auth, basic_service)
    client.put("/v1/admin/config/fees", json={"pct": "0", "fixed": "0"}, headers=admin)
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "24.00"


def test_fee_alone_bumps_tight_spread(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """No floor configured at all: the fee IS the floor. A payout within the
    fee of the buyer price forces a bump — the platform never signs a
    money-losing job."""
    _available(client, auth, basic_service)
    # Push the payout to ~19.6 via a tier multiplier so base spread 0.4 < fee 0.88.
    client.put(
        "/v1/admin/config/adjuster_params/seller_tier_multiplier",
        json={"tiers": {"standard": 1.4}},
        headers=admin,
    )
    client.put(
        "/v1/admin/config/pipelines/" + basic_service,
        json={"buyer": [], "seller": ["seller_tier_multiplier"]},
        headers=admin,
    )
    price = _quote_price(client, basic_service, auth)
    assert price != "20.00"  # bumped
    # invariant, not a magic number: spread >= fee at the final price
    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert Decimal(price) - Decimal("19.60") >= estimated_fee(Decimal(price), stripe)


def test_ceiling_still_rejects_on_final_target(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    client.put(
        "/v1/admin/config/margin_floor",
        json={"absolute": 100, "ceiling_multiplier": 1.5},
        headers=admin,
    )
    _available(client, auth, basic_service)
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "a")
    )
    assert r.status_code == 422
    assert "ceiling" in r.json()["detail"]
