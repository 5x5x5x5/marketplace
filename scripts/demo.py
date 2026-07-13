"""Runnable end-to-end demo — the whole lifecycle, headless, no server needed.

    uv run python scripts/demo.py

Uses an in-process TestClient against a throwaway SQLite database, so it needs
no Postgres and no running server. Mirror these calls against a real deployment
by swapping the client for httpx pointed at your host.
"""

import os
import tempfile
from uuid import UUID

# Point at a throwaway DB before importing the app.
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mkdtemp()}/demo.db")
os.environ.setdefault("MARKETPLACE_SECRET", "demo-secret")

from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace import api
from marketplace.auth import mint_token
from marketplace.db import SessionLocal, init_db
from marketplace.entities import Payment
from marketplace.models import PaymentStatus
from marketplace.payments import fake_provider


def bearer(role: str, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token(role, sub)}"}


def main() -> None:
    init_db()
    c = TestClient(api.app)
    admin = bearer("admin", "ops")
    alice = bearer("buyer", "alice")
    carol = bearer("seller", "carol")
    sid = "rideshare"

    print("1. Admin configures a service type + a surge pipeline")
    c.put(
        f"/v1/admin/config/service_types/{sid}",
        json={"base_buyer_price": 20, "base_seller_payout": 14},
        headers=admin,
    )
    c.put(
        f"/v1/admin/config/pipelines/{sid}",
        json={"buyer": ["surge_by_demand_ratio"], "seller": []},
        headers=admin,
    )
    c.put("/v1/admin/config/margin_floor", json={"absolute": 3}, headers=admin)

    print("2. Seller Carol (capacity 2) onboards for payments, then posts availability")
    c.put("/v1/admin/sellers/carol", json={"capacity": 2}, headers=admin)
    onboard = c.post("/v1/seller/payments/onboard", headers=carol).json()
    print(f"   seller onboarded: payments_ready={onboard['payments_ready']}")
    c.post("/v1/seller/availability", json={"service_type_id": sid}, headers=carol)

    print("3. Buyer Alice requests a quote")
    quote = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    print(f"   quote buyer_price = {quote['buyer_price']}")

    print("4. Alice creates a job — the platform offers it to a seller")
    job = c.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=alice).json()
    job_id = job["id"]
    print(f"   job status = {job['status']}")

    print("5. Carol sees the offer (no buyer_price) and accepts — charge succeeds inline")
    offer = c.get("/v1/seller/offers", headers=carol).json()[0]
    print(f"   offer seller_payout = {offer['seller_payout']}  (buyer_price hidden)")
    accepted = c.post(f"/v1/seller/offers/{offer['id']}/accept", headers=carol).json()
    print(f"   job status = {accepted['status']}  (fake provider charges instantly)")

    print("6. Carol completes the job — transaction booked, payout transferred")
    tx = c.post(f"/v1/seller/jobs/{job_id}/complete", headers=carol).json()
    print(f"   margin (platform spread) = {tx['margin']}")

    print("7. Alice reviews Carol")
    c.post(f"/v1/jobs/{job_id}/review", json={"rating": 5, "comment": "great"}, headers=alice)

    print("8. Admin margin summary")
    summary = c.get("/v1/admin/margins/summary", headers=admin).json()
    print(f"   {summary}")

    # --- Act 2: async payment (what a real Stripe confirmation looks like) ---
    print("9. Alice orders again; this time the charge comes back PENDING")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    quote2 = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    job2 = c.post("/v1/jobs", json={"quote_id": quote2["id"]}, headers=alice).json()
    job2_id = job2["id"]
    offer2 = c.get("/v1/seller/offers", headers=carol).json()[0]
    accepted2 = c.post(f"/v1/seller/offers/{offer2['id']}/accept", headers=carol).json()
    print(f"   job status = {accepted2['status']}  (capacity held while awaiting payment)")

    view2 = c.get(f"/v1/jobs/{job2_id}", headers=alice).json()
    print(f"   payment_status={view2['payment_status']}  client_secret={view2['client_secret']}")

    print("10. The provider confirms the charge out-of-band; its webhook lands")
    with SessionLocal() as s:
        provider_payment_id = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(job2_id))
        )
    webhook = c.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_demo_1",
            "kind": "payment_succeeded",
            "object_id": provider_payment_id,
        },
    ).json()
    print(f"   webhook response = {webhook}")
    view2 = c.get(f"/v1/jobs/{job2_id}", headers=alice).json()
    print(f"   job status after webhook = {view2['status']}")

    print("11. Carol completes job 2 — payout transfers to the seller")
    c.post(f"/v1/seller/jobs/{job2_id}/complete", headers=carol)
    payouts = c.get("/v1/admin/payouts", headers=admin).json()
    payout2 = next(p for p in payouts if p["job_id"] == job2_id)
    print(f"   payout status = {payout2['status']}")

    assert onboard["payments_ready"] is True
    assert view2["status"] == "accepted"
    assert payout2["status"] == "paid"
    print("\nAll asserts passed: onboarding ready, async accept resolved via webhook, payout paid.")


if __name__ == "__main__":
    main()
