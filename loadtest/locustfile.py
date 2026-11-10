"""Locust scenarios for the capacity baseline.

Select scenarios by user-class name (not tags), so every selected class
always has runnable tasks:

    uv run locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8123 \
        -u 25 -r 25 -t 60s --csv loadtest/results/browse-25 BrowseUser

    ... LifecycleBuyer LifecycleSeller   # authed quote -> job -> accept -> complete
    ... LoginUser                        # argon2 login hammering

Requires a token file from `loadtest/seed_loadtest.py` (default
`loadtest/.tokens.json`; override with LOADTEST_TOKENS). Expected domain
outcomes under contention (409 capacity/margin-floor, 410 consumed quote)
are recorded as successes, so the failure column in the CSVs means
transport errors, 5xx, or unexpected 4xx only.
"""

import itertools
import json
import os
import random
from pathlib import Path
from typing import Any

from locust import HttpUser, constant, task

_TOKENS_PATH = Path(os.environ.get("LOADTEST_TOKENS", Path(__file__).parent / ".tokens.json"))
_TOKENS = json.loads(_TOKENS_PATH.read_text())
_SERVICE_TYPES: list[str] = _TOKENS["service_type_ids"]
_buyer_cycle = itertools.cycle(_TOKENS["buyers"])
_seller_cycle = itertools.cycle(_TOKENS["sellers"])
_login_cycle = itertools.cycle(_TOKENS["buyers"] + _TOKENS["sellers"])

# Matching funnels offers to the oldest-available sellers, so a seller user
# that polled only one account would usually see nothing. Each seller user
# round-robins over a small slice of accounts instead; slices are handed out
# from the head of the seeded list, where the funnel lands first.
_SELLERS_PER_USER = int(os.environ.get("LOADTEST_SELLERS_PER_USER", "5"))
# Share of quotes pinned to a specific catalog listing (the "book this seller"
# path) rather than left to platform dispatch.
_PINNED_SHARE = float(os.environ.get("LOADTEST_PINNED_SHARE", "0.3"))


def _bearer(account: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['token']}"}


class BrowseUser(HttpUser):
    """Anonymous read hot paths: public catalog JSON and the server-rendered
    pages built from the same views."""

    wait_time = constant(0)

    def on_start(self) -> None:
        rows: list[dict[str, Any]] = self.client.get(
            "/v1/catalog/listings", params={"limit": 50}, name="/v1/catalog/listings"
        ).json()
        self.listing_ids = [row["id"] for row in rows] or [""]
        self.seller_ids = list({row["seller"]["seller_id"] for row in rows}) or [""]

    @task(5)
    def catalog_json(self) -> None:
        self.client.get("/v1/catalog/listings", params={"limit": 50})

    @task(2)
    def catalog_json_filtered(self) -> None:
        self.client.get(
            "/v1/catalog/listings",
            params={"limit": 20, "service_type_id": random.choice(_SERVICE_TYPES)},
            name="/v1/catalog/listings?service_type_id",
        )

    @task(3)
    def catalog_detail(self) -> None:
        self.client.get(
            f"/v1/catalog/listings/{random.choice(self.listing_ids)}",
            name="/v1/catalog/listings/{id}",
        )

    @task(1)
    def catalog_seller(self) -> None:
        self.client.get(
            f"/v1/catalog/sellers/{random.choice(self.seller_ids)}",
            name="/v1/catalog/sellers/{id}",
        )

    @task(3)
    def landing_page(self) -> None:
        self.client.get("/")

    @task(2)
    def listing_page(self) -> None:
        self.client.get(f"/listings/{random.choice(self.listing_ids)}", name="/listings/{id}")

    @task(1)
    def seller_page(self) -> None:
        self.client.get(f"/sellers/{random.choice(self.seller_ids)}", name="/sellers/{id}")


class LifecycleBuyer(HttpUser):
    """Quote -> job, exercising the quote sweep, candidate loading, and the
    matching walk. Uses a pre-seeded bearer token (no argon2 here)."""

    weight = 1
    wait_time = constant(0.2)

    def on_start(self) -> None:
        self.headers = _bearer(next(_buyer_cycle))
        rows: list[dict[str, Any]] = self.client.get(
            "/v1/catalog/listings", params={"limit": 50}, name="/v1/catalog/listings"
        ).json()
        self.listing_ids = [row["id"] for row in rows]

    @task(5)
    def quote_then_job(self) -> None:
        body: dict[str, str]
        if self.listing_ids and random.random() < _PINNED_SHARE:
            body = {"listing_id": random.choice(self.listing_ids)}
        else:
            body = {"service_type_id": random.choice(_SERVICE_TYPES)}
        quote = self.client.post(
            "/v1/quotes", json=body, headers=self.headers, name="/v1/quotes", catch_response=True
        )
        with quote:
            if quote.status_code == 201:
                quote.success()
            elif quote.status_code == 409:
                # No margin-clearing candidate / listing unavailable: domain outcome.
                quote.success()
                return
            else:
                quote.failure(f"unexpected {quote.status_code}")
                return
        with self.client.post(
            "/v1/jobs",
            json={"quote_id": quote.json()["id"]},
            headers=self.headers,
            name="/v1/jobs",
            catch_response=True,
        ) as resp:
            # 409 (no capacity anywhere) and 410 (quote raced/expired or the
            # pinned listing went away) are domain outcomes under load.
            if resp.status_code in (201, 409, 410):
                resp.success()

    @task(1)
    def poll_jobs(self) -> None:
        self.client.get("/v1/jobs", headers=self.headers)


class LifecycleSeller(HttpUser):
    """Offer poll -> accept -> complete: the row-locked accept (charge inside
    the lock) and completion (transfer + margin booking)."""

    weight = 2  # matching funnels offers to few sellers; give accepts headroom
    wait_time = constant(0.2)

    def on_start(self) -> None:
        self.accounts = [next(_seller_cycle) for _ in range(_SELLERS_PER_USER)]
        self._turn = itertools.cycle(self.accounts)

    @task(3)
    def poll_and_accept(self) -> None:
        headers = _bearer(next(self._turn))
        offers = self.client.get(
            "/v1/seller/offers", headers=headers, name="/v1/seller/offers"
        ).json()
        offered = [o for o in offers if o["status"] == "offered"]
        for offer in offered[:5]:
            with self.client.post(
                f"/v1/seller/offers/{offer['id']}/accept",
                headers=headers,
                name="/v1/seller/offers/{id}/accept",
                catch_response=True,
            ) as resp:
                # 409: capacity full or offer raced by the sweep; 410: expired.
                if resp.status_code in (200, 409, 410):
                    resp.success()
                if resp.status_code == 409:
                    break  # capacity full; stop burning attempts this poll

    @task(2)
    def complete_accepted(self) -> None:
        headers = _bearer(next(self._turn))
        jobs = self.client.get("/v1/seller/jobs", headers=headers, name="/v1/seller/jobs").json()
        accepted = [j for j in jobs if j["status"] == "accepted"]
        for job in accepted[:3]:
            with self.client.post(
                f"/v1/seller/jobs/{job['id']}/complete",
                headers=headers,
                name="/v1/seller/jobs/{id}/complete",
                catch_response=True,
            ) as resp:
                if resp.status_code in (200, 409):
                    resp.success()


class LoginUser(HttpUser):
    """Pure argon2 pressure: every request is a real login."""

    wait_time = constant(0)

    @task
    def login(self) -> None:
        account = next(_login_cycle)
        self.client.post(
            "/v1/auth/login",
            json={
                "email": account["email"],
                "password": account["password"],
                "role": account["role"],
            },
            name="/v1/auth/login",
        )
