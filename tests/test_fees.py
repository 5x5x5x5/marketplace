"""Fee-aware margin: config plumbing, pure fee math, admin endpoint."""

from decimal import Decimal

from fastapi.testclient import TestClient

from marketplace.config import FeeConfig, MarginFloor
from marketplace.db import SessionLocal
from marketplace.matching import estimated_fee, required_spread
from marketplace.repo import fee_config, get_platform_config
from tests.conftest import Header


def test_estimated_fee_math() -> None:
    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert estimated_fee(Decimal("50.00"), stripe) == Decimal("1.75")
    assert estimated_fee(Decimal("20.00"), stripe) == Decimal("0.88")
    # half-up quantization at the money boundary
    assert estimated_fee(Decimal("25.00"), stripe) == Decimal("1.03")  # 1.025 rounds up
    # zero config means zero fee
    assert estimated_fee(Decimal("50.00"), FeeConfig()) == Decimal("0.00")


def test_required_spread_is_floor_plus_fee() -> None:
    floor = MarginFloor(absolute=Decimal("10"))
    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert required_spread(Decimal("20.00"), floor, stripe) == Decimal("10.88")
    # with zero fees it degrades to the old effective floor
    assert required_spread(Decimal("20.00"), floor, FeeConfig()) == Decimal("10.00")


def test_fee_config_row_defaults() -> None:
    """A fresh platform row defaults to Stripe's standard card rate."""
    with SessionLocal() as session:
        get_platform_config(session)
        fees = fee_config(session)
        session.commit()
    assert fees.pct == Decimal("0.029")
    assert fees.fixed == Decimal("0.30")


def test_admin_fees_endpoint_roundtrip_and_audit(client: TestClient, admin: Header) -> None:
    r = client.put("/v1/admin/config/fees", json={"pct": "0.02", "fixed": "0.25"}, headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert Decimal(body["pct"]) == Decimal("0.02")
    assert Decimal(body["fixed"]) == Decimal("0.25")
    cfg = client.get("/v1/admin/config", headers=admin).json()
    assert Decimal(cfg["fees"]["pct"]) == Decimal("0.02")
    assert Decimal(cfg["fees"]["fixed"]) == Decimal("0.25")
    audit_rows = client.get("/v1/admin/audit", headers=admin).json()
    assert any(a["action"] == "update_fees" for a in audit_rows)


def test_admin_fees_endpoint_validation(client: TestClient, admin: Header) -> None:
    for bad in ({"pct": "1"}, {"pct": "-0.01"}, {"fixed": "-1"}):
        r = client.put("/v1/admin/config/fees", json=bad, headers=admin)
        assert r.status_code == 422, bad
