"""Drive the money lifecycle against a RUNNING server over real HTTP.

    uv run python .claude/skills/tryout/probe.py [--base-url URL] [--iterations N]

Checks the bug classes that only a network client can see (TestClient awaits
the whole app cycle, teardown included, so it is blind to them):

- read-your-writes: a fresh quote/job is usable on the very next call (F2)
- idempotency replay: an immediate same-key retry replays byte-identical (F2b)
- asymmetry: no buyer response carries seller_payout/margin, no seller
  response carries buyer_price/margin/buyer_id (F1), and the public landing
  page source carries none of the catalog-forbidden strings
- money identity: each booked transaction's margin == buyer_price -
  seller_payout, and both sides match what buyer and seller were shown

It creates its own service type (probe-<hex>) with one fresh seller, so the
offer cannot go to some other seeded seller, and fresh buyer/seller accounts.
Admin credentials default to scripts/seed.py's defaults; pass the ones the
server was started with (ADMIN_EMAIL/ADMIN_PASSWORD) otherwise.

Prints findings in the docs/tryout-findings-*.md entry shape and exits 1 if
there are any. Uses httpx (a dev dependency) only.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from decimal import Decimal
from typing import Any

import httpx

BUYER_FORBIDDEN = {"seller_payout", "margin"}
SELLER_FORBIDDEN = {"buyer_price", "margin", "buyer_id", "email"}
PAGE_FORBIDDEN = ("seller_payout", "margin", "capacity", "payments_ready")


class Probe:
    def __init__(self, base_url: str) -> None:
        self.c = httpx.Client(base_url=base_url, timeout=15)
        self.findings: list[dict[str, str]] = []

    def finding(self, title: str, surfaced: str, where: str, severity: str) -> None:
        if any(f["title"] == title and f["where"] == where for f in self.findings):
            return  # one entry per defect, not one per iteration
        self.findings.append(
            {"title": title, "surfaced": surfaced, "where": where, "severity": severity}
        )

    def call(self, method: str, path: str, token: str | None = None, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.c.request(method, path, headers=headers, **kw)

    def must(self, resp: httpx.Response, *ok: int) -> Any:
        if resp.status_code not in ok:
            raise SystemExit(
                f"setup call failed: {resp.request.method} {resp.request.url.path} -> "
                f"{resp.status_code} {resp.text[:300]}\n"
                "If a 401/404 names a token or resource the previous response just returned, "
                "that is itself a finding: the read-your-writes class (F2), the server answered "
                "before its commit landed."
            )
        return resp.json()

    def scan(self, body: Any, forbidden: set[str], role: str, where: str) -> None:
        found = sorted(keys(body) & forbidden)
        if found:
            self.finding(
                f"{role} response carries {', '.join(found)}",
                f"{where} returned key(s) {found} to a {role}",
                where,
                "Critical-class (information asymmetry)",
            )


def keys(body: Any) -> set[str]:
    if isinstance(body, dict):
        out = set(body)
        for value in body.values():
            out |= keys(value)
        return out
    if isinstance(body, list):
        out: set[str] = set()
        for item in body:
            out |= keys(item)
        return out
    return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--iterations", type=int, default=30)
    ap.add_argument(
        "--admin-email", default=os.environ.get("ADMIN_EMAIL", "admin@marketplace-demo.dev")
    )
    ap.add_argument(
        "--admin-password", default=os.environ.get("ADMIN_PASSWORD", "try-it-admin-password")
    )
    args = ap.parse_args()
    p = Probe(args.base_url)
    tag = uuid.uuid4().hex[:8]

    p.must(p.call("GET", "/healthz"), 200)
    admin = p.must(
        p.call(
            "POST",
            "/v1/auth/login",
            json={"email": args.admin_email, "password": args.admin_password, "role": "admin"},
        ),
        200,
    )["token"]

    sid = f"probe-{tag}"
    p.must(
        p.call(
            "PUT",
            f"/v1/admin/config/service_types/{sid}",
            admin,
            json={"base_buyer_price": 20, "base_seller_payout": 14},
        ),
        200,
    )
    p.must(
        p.call("PUT", f"/v1/admin/config/pipelines/{sid}", admin, json={"buyer": [], "seller": []}),
        200,
    )

    def signup(role: str) -> tuple[str, str]:
        body = p.must(
            p.call(
                "POST",
                "/v1/auth/signup",
                json={
                    "email": f"probe-{tag}-{role}@tryout-probe.dev",
                    "password": "probe-password-1",
                    "role": role,
                    "display_name": f"Probe {role}",
                },
            ),
            201,
        )
        return body["token"], body["user"]["id"]

    buyer, _ = signup("buyer")
    seller, seller_id = signup("seller")
    p.must(
        p.call("PUT", f"/v1/admin/sellers/{seller_id}", admin, json={"capacity": args.iterations}),
        200,
    )
    p.must(p.call("POST", "/v1/seller/payments/onboard", seller), 200)
    p.must(p.call("POST", "/v1/seller/availability", seller, json={"service_type_id": sid}), 200)

    shown: dict[str, tuple[Decimal, Decimal]] = {}
    stale_reads = 0
    for i in range(args.iterations):
        q = p.call("POST", "/v1/quotes", buyer, json={"service_type_id": sid})
        if q.status_code != 201:
            p.finding(
                "quote failed",
                f"iteration {i}: {q.status_code} {q.text[:200]}",
                "POST /v1/quotes",
                "Important",
            )
            continue
        p.scan(q.json(), BUYER_FORBIDDEN, "buyer", "POST /v1/quotes")
        j = p.call("POST", "/v1/jobs", buyer, json={"quote_id": q.json()["id"]})
        if j.status_code == 404:
            stale_reads += 1
            continue
        if j.status_code != 201:
            p.finding(
                "job create failed",
                f"iteration {i}: {j.status_code} {j.text[:200]}",
                "POST /v1/jobs",
                "Important",
            )
            continue
        job = j.json()
        p.scan(job, BUYER_FORBIDDEN, "buyer", "POST /v1/jobs")
        offers = p.must(p.call("GET", "/v1/seller/offers", seller), 200)
        p.scan(offers, SELLER_FORBIDDEN, "seller", "GET /v1/seller/offers")
        offer = next((o for o in offers if o["job_id"] == job["id"]), None)
        if offer is None:
            stale_reads += 1
            continue
        acc = p.call("POST", f"/v1/seller/offers/{offer['id']}/accept", seller)
        if acc.status_code != 200:
            p.finding(
                "accept failed",
                f"iteration {i}: {acc.status_code} {acc.text[:200]}",
                "POST /v1/seller/offers/{id}/accept",
                "Important",
            )
            continue
        p.scan(acc.json(), SELLER_FORBIDDEN, "seller", "POST /v1/seller/offers/{id}/accept")
        done = p.call("POST", f"/v1/seller/jobs/{job['id']}/complete", seller)
        if done.status_code != 200:
            if done.status_code == 404:
                stale_reads += 1
            else:
                p.finding(
                    "complete failed",
                    f"iteration {i}: {done.status_code} {done.text[:200]}",
                    "POST /v1/seller/jobs/{id}/complete",
                    "Important",
                )
            continue
        p.scan(done.json(), SELLER_FORBIDDEN, "seller", "POST /v1/seller/jobs/{id}/complete")
        shown[job["id"]] = (Decimal(job["buyer_price"]), Decimal(done.json()["seller_payout"]))

    if stale_reads:
        p.finding(
            "chained call saw uncommitted state",
            f"{stale_reads}/{args.iterations} chained calls 404'd on a resource the previous "
            "response had just returned",
            "commit ordering (db.CommitRoute)",
            "Critical-class (read-your-writes)",
        )

    key = {"Idempotency-Key": f"probe-{tag}-replay"}
    first = p.call("POST", "/v1/quotes", buyer, json={"service_type_id": sid}, headers=key)
    second = p.call("POST", "/v1/quotes", buyer, json={"service_type_id": sid}, headers=key)
    if (first.status_code, first.content) != (second.status_code, second.content):
        p.finding(
            "same-key retry did not replay",
            f"first {first.status_code}, retry {second.status_code}; bodies differ",
            "idempotency.py (buffer, store, then send)",
            "Important",
        )

    page = p.call("GET", "/")
    if page.status_code == 200:
        leaked = [s for s in PAGE_FORBIDDEN if s in page.text]
        if leaked:
            p.finding(
                "public page source leaks",
                f"GET / contains {leaked}",
                "templates / catalog.py",
                "Critical-class",
            )

    ledger = p.must(p.call("GET", "/v1/admin/transactions?limit=200", admin), 200)
    booked = {t["job_id"]: t for t in ledger if t["job_id"] in shown}
    for job_id, (price, payout) in shown.items():
        t = booked.get(job_id)
        if t is None:
            p.finding("completed job has no transaction", job_id, "complete_job", "Critical")
            continue
        bp, sp, m = Decimal(t["buyer_price"]), Decimal(t["seller_payout"]), Decimal(t["margin"])
        if (bp, sp) != (price, payout) or m != bp - sp:
            p.finding(
                "money identity broken",
                f"job {job_id}: ledger {bp}/{sp}/{m}, buyer saw {price}, seller saw {payout}",
                "Transaction booking",
                "Critical",
            )

    print(
        f"probe {tag}: {len(shown)}/{args.iterations} lifecycles completed against {args.base_url}"
    )
    for n, f in enumerate(p.findings, 1):
        print(
            f"\n## P{n} — {f['title']}\n\n- **Surfaced:** {f['surfaced']}\n"
            f"- **Where:** {f['where']}\n- **Severity:** {f['severity']}\n- **Status:** OPEN"
        )
    if not p.findings:
        print("no findings: read-your-writes, replay, asymmetry and money identity held")
    return 1 if p.findings else 0


if __name__ == "__main__":
    sys.exit(main())
