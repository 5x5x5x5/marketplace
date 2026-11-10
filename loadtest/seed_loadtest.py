"""Seed the database at load-test scale and write bearer tokens for Locust.

    uv run python loadtest/seed_loadtest.py [--sellers 50] [--buyers 10]
        [--catalog-size 100] [--capacity 3] [--tokens loadtest/.tokens.json]

Creates two service types with pricing, N sellers (onboarded for payments via
the fake provider, available for both service types, admin-set capacity) and
M buyers, plus enough listings that the public catalog holds at least
`--catalog-size` rows (spread evenly over sellers and service types). Everything
goes through the real endpoints via an in-process TestClient, so the app's own
validation applies and no server needs to be up; it writes to whatever
DATABASE_URL points at.

Idempotent: signup falls back to login, listings are skipped by title, tokens
are refreshed on every run. Re-running with a larger `--catalog-size` grows the
catalog; it never shrinks it.

Writes the token file (gitignored) with admin/buyer/seller bearer tokens and
login credentials so Locust users skip argon2 except in the login scenario.
"""

import argparse
import json
import logging
import math
import os
from pathlib import Path

os.environ.setdefault("ADMIN_EMAIL", "admin@loadtest.dev")
os.environ.setdefault("ADMIN_PASSWORD", "load-test-admin-password")
# Real env vars outrank .env: pin these empty so a developer's .env (with a
# real Stripe key) can never make seeding hit the live Stripe API.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")
os.environ.setdefault("SMTP_HOST", "")

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace import api
from marketplace.db import SessionLocal, init_db
from marketplace.entities import Listing
from marketplace.settings import settings

logging.disable(logging.INFO)

PASSWORD = "load-test-password-1"
SERVICE_TYPE_IDS = ["rideshare", "cleaning"]
PRICES: dict[str, dict[str, float]] = {
    "rideshare": {"base_buyer_price": 20, "base_seller_payout": 14},
    "cleaning": {"base_buyer_price": 80, "base_seller_payout": 55},
}
PIPELINES: dict[str, dict[str, list[str]]] = {
    "rideshare": {"buyer": ["surge_by_demand_ratio"], "seller": []},
    "cleaning": {"buyer": [], "seller": ["seller_tier_multiplier"]},
}
DEFAULT_TOKENS = Path(__file__).parent / ".tokens.json"

Account = dict[str, str]


def _signup_or_login(c: TestClient, email: str, role: str) -> tuple[str, str]:
    body = {"email": email, "password": PASSWORD, "role": role}
    resp = c.post("/v1/auth/signup", json={**body, "display_name": f"Load {role}"})
    if resp.status_code == 409:
        resp = c.post("/v1/auth/login", json=body)
    assert resp.status_code in (200, 201), f"{role} auth failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["token"], data["user"]["id"]


def _catalog_size() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(Listing)) or 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sellers", type=int, default=50)
    parser.add_argument("--buyers", type=int, default=10)
    parser.add_argument(
        "--catalog-size",
        type=int,
        default=None,
        help="minimum number of listings (default: one per seller per service type)",
    )
    parser.add_argument("--capacity", type=int, default=3)
    parser.add_argument("--tokens", type=Path, default=DEFAULT_TOKENS)
    args = parser.parse_args()

    slots = args.sellers * len(SERVICE_TYPE_IDS)
    target = args.catalog_size if args.catalog_size is not None else slots
    per_slot = max(1, math.ceil(target / slots))

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
            "to the values your server was started with."
        )
        admin_token = admin_resp.json()["token"]
        admin = {"Authorization": f"Bearer {admin_token}"}

        for sid in SERVICE_TYPE_IDS:
            resp = c.put(f"/v1/admin/config/service_types/{sid}", json=PRICES[sid], headers=admin)
            assert resp.status_code == 200, resp.text
            resp = c.put(f"/v1/admin/config/pipelines/{sid}", json=PIPELINES[sid], headers=admin)
            assert resp.status_code == 200, resp.text
        resp = c.put("/v1/admin/config/margin_floor", json={"absolute": 3}, headers=admin)
        assert resp.status_code == 200, resp.text

        buyers: list[Account] = []
        for i in range(args.buyers):
            email = f"loadbuyer{i}@loadtest.dev"
            token, user_id = _signup_or_login(c, email, "buyer")
            buyers.append(
                {
                    "email": email,
                    "token": token,
                    "id": user_id,
                    "password": PASSWORD,
                    "role": "buyer",
                }
            )

        sellers: list[Account] = []
        for i in range(args.sellers):
            email = f"loadseller{i}@loadtest.dev"
            token, user_id = _signup_or_login(c, email, "seller")
            headers = {"Authorization": f"Bearer {token}"}
            resp = c.put(
                f"/v1/admin/sellers/{user_id}", json={"capacity": args.capacity}, headers=admin
            )
            assert resp.status_code == 200, resp.text
            onboard = c.post("/v1/seller/payments/onboard", headers=headers).json()
            assert onboard["payments_ready"] is True, onboard
            for sid in SERVICE_TYPE_IDS:
                resp = c.post(
                    "/v1/seller/availability", json={"service_type_id": sid}, headers=headers
                )
                assert resp.status_code == 200, resp.text
            existing = {
                row["title"] for row in c.get("/v1/seller/listings", headers=headers).json()
            }
            for sid in SERVICE_TYPE_IDS:
                for j in range(per_slot):
                    title = f"Load {sid} {i}-{j}"
                    if title in existing:
                        continue
                    resp = c.post(
                        "/v1/seller/listings",
                        json={
                            "service_type_id": sid,
                            "title": title,
                            "description": "Seeded for load testing.",
                            "photo_urls": ["https://placehold.co/600x400"],
                        },
                        headers=headers,
                    )
                    assert resp.status_code == 201, resp.text
            sellers.append(
                {
                    "email": email,
                    "token": token,
                    "id": user_id,
                    "password": PASSWORD,
                    "role": "seller",
                }
            )

    catalog_size = _catalog_size()
    args.tokens.write_text(
        json.dumps(
            {
                "admin_token": admin_token,
                "service_type_ids": SERVICE_TYPE_IDS,
                "buyers": buyers,
                "sellers": sellers,
                "catalog_size": catalog_size,
            },
            indent=2,
        )
    )
    db_kind = settings.database_url.split("://", 1)[0]
    print(
        f"Seeded ({db_kind}): {len(buyers)} buyers, {len(sellers)} sellers, "
        f"{catalog_size} listings; tokens -> {args.tokens}"
    )


if __name__ == "__main__":
    main()
