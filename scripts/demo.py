"""Runnable end-to-end demo — the whole lifecycle, headless, no server needed.

    uv run python scripts/demo.py

Uses an in-process TestClient against a throwaway SQLite database, so it needs
no Postgres and no running server. Mirror these calls against a real deployment
by swapping the client for httpx pointed at your host.
"""

import os
import tempfile

# Point at a throwaway DB before importing the app.
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mkdtemp()}/demo.db")
os.environ.setdefault("MARKETPLACE_SECRET", "demo-secret")

from fastapi.testclient import TestClient

from marketplace import api
from marketplace.auth import mint_token
from marketplace.db import init_db


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

    print("2. Seller Carol (capacity 2) posts availability")
    c.put("/v1/admin/sellers/carol", json={"capacity": 2}, headers=admin)
    c.post("/v1/seller/availability", json={"service_type_id": sid}, headers=carol)

    print("3. Buyer Alice requests a quote")
    quote = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    print(f"   quote buyer_price = {quote['buyer_price']}")

    print("4. Alice creates a job — the platform offers it to a seller")
    job = c.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=alice).json()
    job_id = job["id"]
    print(f"   job status = {job['status']}")

    print("5. Carol sees the offer (no buyer_price) and accepts")
    offer = c.get("/v1/seller/offers", headers=carol).json()[0]
    print(f"   offer seller_payout = {offer['seller_payout']}  (buyer_price hidden)")
    c.post(f"/v1/seller/offers/{offer['id']}/accept", headers=carol)

    print("6. Carol completes the job — transaction booked")
    tx = c.post(f"/v1/seller/jobs/{job_id}/complete", headers=carol).json()
    print(f"   margin (platform spread) = {tx['margin']}")

    print("7. Alice reviews Carol")
    c.post(f"/v1/jobs/{job_id}/review", json={"rating": 5, "comment": "great"}, headers=alice)

    print("8. Admin margin summary")
    summary = c.get("/v1/admin/margins/summary", headers=admin).json()
    print(f"   {summary}")


if __name__ == "__main__":
    main()
