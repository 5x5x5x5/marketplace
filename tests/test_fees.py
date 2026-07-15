"""Fee-aware margin: config plumbing, pure fee math, admin endpoint."""

from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.config import FeeConfig, MarginFloor
from marketplace.db import SessionLocal
from marketplace.entities import Payment
from marketplace.matching import estimated_fee, required_spread
from marketplace.models import PaymentStatus, to_money
from marketplace.payments import fake_provider
from marketplace.repo import fee_config, get_platform_config
from tests.conftest import AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail


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


def test_charge_stamps_fee_snapshot(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """The estimate is stamped from config at charge time; later config
    changes never rewrite it."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))

    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(str(job["id"])))).one()
        stamped = payment.fee_estimate
        price = payment.amount
    assert stamped == to_money(price * Decimal("0.029") + Decimal("0.30"))

    client.put("/v1/admin/config/fees", json={"pct": "0.10", "fixed": "5"}, headers=admin)
    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(str(job["id"])))).one()
        assert payment.fee_estimate == stamped  # snapshot, not live


def test_summary_counts_refunded_fee_the_roadmap_hole(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Charge → admin cancels the paid job (buyers can't cancel a job that's
    already accepted+paid — only an admin unwinds it). No Transaction ever
    books, but the fee was still paid: the summary must show it."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    r = client.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin)
    assert r.status_code == 200

    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(str(job["id"])))).one()
        assert payment.status == PaymentStatus.REFUNDED
        fee = payment.fee_estimate

    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(summary["fees_estimated"]) == fee
    assert Decimal(summary["platform_margin"]) == Decimal("0.00")
    assert Decimal(summary["platform_margin_net_of_fees"]) == -fee


def test_summary_excludes_uncaptured_charges(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A PENDING (unconfirmed) charge has cost nothing yet — excluded."""
    onboard_and_avail(client, auth, basic_service, "s1")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    assert job["id"]  # charge parked AWAITING_PAYMENT

    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(summary["fees_estimated"]) == Decimal("0.00")


def test_summary_net_math_exact(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))

    s = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(s["fees_estimated"]) > 0
    assert Decimal(s["platform_margin_net_of_fees"]) == (
        Decimal(s["platform_margin"]) + Decimal(s["adjustments_net"]) - Decimal(s["fees_estimated"])
    )
