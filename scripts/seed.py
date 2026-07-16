"""Seed the database so /docs is playable immediately.

    ADMIN_EMAIL=admin@marketplace-demo.dev ADMIN_PASSWORD=change-me \
        uv run python scripts/seed.py

Writes to whatever DATABASE_URL points at (the same ./marketplace.db the dev
server uses by default), creating two service types with pipelines, a demo
buyer, and a demo seller (onboarded, available, capacity 2), then prints the
three bearer tokens to paste into Swagger's Authorize dialog.

Idempotent: service-type/pipeline PUTs are upserts, availability is an
upsert, and an existing demo user falls back to login — re-running refreshes
the printed tokens (sessions expire after SESSION_TTL_HOURS, default 72).

ponytail: in-process TestClient, not httpx-to-a-running-server — it reuses
the API's own validation and needs no server up; run it before or after
starting uvicorn, against the same DATABASE_URL.
"""

import logging
import os

# The admin account is seeded from these at startup; defaults keep the
# out-of-the-box flow one command, but match your server's env if you set one.
os.environ.setdefault("ADMIN_EMAIL", "admin@marketplace-demo.dev")
os.environ.setdefault("ADMIN_PASSWORD", "try-it-admin-password")
# Real env vars outrank .env in pydantic-settings: pin these empty so a
# developer's .env (with a real Stripe key) can never make seeding hit the
# live Stripe API — seeded onboarding uses the deterministic fake provider.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")

from fastapi.testclient import TestClient

from marketplace import api
from marketplace.db import init_db
from marketplace.settings import settings

# ponytail: the tokens are the output; in-process access logs would drown them
logging.disable(logging.INFO)

BUYER_EMAIL = "buyer@marketplace-demo.dev"
SELLER_EMAIL = "seller@marketplace-demo.dev"
DEMO_PASSWORD = "try-it-password-1"

SERVICE_TYPES: dict[str, dict[str, object]] = {
    "rideshare": {
        "prices": {"base_buyer_price": 20, "base_seller_payout": 14},
        "pipelines": {"buyer": ["surge_by_demand_ratio"], "seller": []},
    },
    "cleaning": {
        "prices": {"base_buyer_price": 80, "base_seller_payout": 55},
        "pipelines": {"buyer": [], "seller": ["seller_tier_multiplier"]},
    },
}


def _signup_or_login(c: TestClient, email: str, role: str) -> tuple[str, str]:
    """Returns (token, user_id); tolerates the user already existing."""
    body = {"email": email, "password": DEMO_PASSWORD, "role": role}
    resp = c.post("/v1/auth/signup", json={**body, "display_name": role.capitalize()})
    if resp.status_code == 409:
        resp = c.post("/v1/auth/login", json=body)
    assert resp.status_code in (200, 201), f"{role} auth failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["token"], data["user"]["id"]


def main() -> None:
    init_db()
    with TestClient(api.app) as c:
        admin_resp = c.post(
            "/v1/auth/login",
            json={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "role": "admin",
            },
        )
        assert admin_resp.status_code == 200, (
            f"admin login failed ({admin_resp.status_code}): set ADMIN_EMAIL/ADMIN_PASSWORD "
            "to the values your server was started with, or unset them to use the defaults."
        )
        admin_token = admin_resp.json()["token"]
        admin = {"Authorization": f"Bearer {admin_token}"}

        for sid, cfg in SERVICE_TYPES.items():
            assert (
                c.put(
                    f"/v1/admin/config/service_types/{sid}", json=cfg["prices"], headers=admin
                ).status_code
                == 200
            )
            assert (
                c.put(
                    f"/v1/admin/config/pipelines/{sid}", json=cfg["pipelines"], headers=admin
                ).status_code
                == 200
            )
        c.put("/v1/admin/config/margin_floor", json={"absolute": 3}, headers=admin)

        buyer_token, _ = _signup_or_login(c, BUYER_EMAIL, "buyer")
        seller_token, seller_id = _signup_or_login(c, SELLER_EMAIL, "seller")

        c.put(f"/v1/admin/sellers/{seller_id}", json={"capacity": 2}, headers=admin)
        seller = {"Authorization": f"Bearer {seller_token}"}
        onboard = c.post("/v1/seller/payments/onboard", headers=seller).json()
        assert onboard["payments_ready"] is True, onboard
        for sid in SERVICE_TYPES:
            assert (
                c.post(
                    "/v1/seller/availability", json={"service_type_id": sid}, headers=seller
                ).status_code
                == 200
            )

    db_kind = settings.database_url.split("://", 1)[0]
    print(f"Seeded ({db_kind}): {', '.join(SERVICE_TYPES)} + demo buyer/seller.\n")
    print("Paste one of these into Swagger's Authorize dialog (just the token,")
    print(f"no Bearer prefix) at http://localhost:8000/docs — valid {settings.session_ttl_hours}h,")
    print("re-run this script for fresh ones:\n")
    print(f"  admin   ({settings.admin_email} / {settings.admin_password}):\n  {admin_token}\n")
    print(f"  buyer   ({BUYER_EMAIL} / {DEMO_PASSWORD}):\n  {buyer_token}\n")
    print(f"  seller  ({SELLER_EMAIL} / {DEMO_PASSWORD}):\n  {seller_token}")


if __name__ == "__main__":
    main()
