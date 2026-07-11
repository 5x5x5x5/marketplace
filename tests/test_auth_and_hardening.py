"""Regression tests for the safe-to-pilot hardening pass.

Each test pins one of the sweep findings shut: authn/authz (C1-C4),
concurrency (H1-H2), and admin-input validation (H3, H5, M1).
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from marketplace import api
from marketplace.models import MarginFloorBody, ServiceTypeBody
from tests.conftest import AuthFactory, Header

# ---------- Authentication / authorization ----------


def test_admin_requires_token(client: TestClient) -> None:
    """C1: /admin/* is closed without an operator token, and to non-admins."""
    r = client.get("/admin/transactions")
    assert r.status_code == 401  # no token
    r = client.get("/admin/transactions", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401  # bad signature


def test_admin_rejects_non_admin_role(client: TestClient, auth: AuthFactory) -> None:
    r = client.get("/admin/transactions", headers=auth("buyer", "alice"))
    assert r.status_code == 403


def test_seller_cannot_read_buyer_view(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """C2: a seller holding the job_id cannot fetch the buyer view (which carries buyer_price)."""
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    quote_id = client.post(
        "/quotes", json={"service_type_id": basic_service}, headers=buyer
    ).json()["id"]
    job_id = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer).json()["id"]

    # Seller learns job_id from /jobs/offered, then tries the buyer endpoint.
    r = client.get(f"/jobs/{job_id}", headers=seller)
    assert r.status_code == 403  # seller role rejected by current_buyer


def test_buyer_cannot_read_seller_payout(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """C3: a buyer cannot enumerate a seller's offered jobs (which carry seller_payout)."""
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    client.post("/availability", json={"service_type_id": basic_service}, headers=seller)
    quote_id = client.post(
        "/quotes", json={"service_type_id": basic_service}, headers=buyer
    ).json()["id"]
    client.post("/jobs", json={"quote_id": quote_id}, headers=buyer)

    r = client.get("/jobs/offered", headers=buyer)
    assert r.status_code == 403  # buyer role rejected by current_seller


def test_cannot_complete_others_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """C4: an authenticated seller cannot complete a job offered to someone else."""
    owner = auth("seller", "owner")
    buyer = auth("buyer", "alice")
    client.post("/availability", json={"service_type_id": basic_service}, headers=owner)
    quote_id = client.post(
        "/quotes", json={"service_type_id": basic_service}, headers=buyer
    ).json()["id"]
    job_id = client.post("/jobs", json={"quote_id": quote_id}, headers=buyer).json()["id"]
    client.post(f"/jobs/{job_id}/accept", headers=owner)

    r = client.post(f"/jobs/{job_id}/complete", headers=auth("seller", "thief"))
    assert r.status_code == 403


# ---------- Concurrency ----------


def _setup_quote(client: TestClient, sid: str, auth: AuthFactory) -> str:
    client.post("/availability", json={"service_type_id": sid}, headers=auth("seller", "s1"))
    return client.post(
        "/quotes", json={"service_type_id": sid}, headers=auth("buyer", "alice")
    ).json()["id"]


def test_concurrent_job_creation_consumes_quote_once(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """H1: N threads racing POST /jobs on one quote yield exactly one job."""
    quote_id = _setup_quote(client, basic_service, auth)
    buyer = auth("buyer", "alice")
    n = 8
    barrier = threading.Barrier(n)

    def attempt(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post("/jobs", json={"quote_id": quote_id}, headers=buyer).status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        codes = list(pool.map(attempt, range(n)))

    assert codes.count(200) == 1, codes
    assert len(api.store.jobs) == 1


def test_concurrent_complete_books_one_transaction(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """H2: N threads racing POST /complete book exactly one transaction."""
    seller = auth("seller", "s1")
    quote_id = _setup_quote(client, basic_service, auth)
    job_id = client.post(
        "/jobs", json={"quote_id": quote_id}, headers=auth("buyer", "alice")
    ).json()["id"]
    client.post(f"/jobs/{job_id}/accept", headers=seller)

    n = 8
    barrier = threading.Barrier(n)

    def attempt(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(f"/jobs/{job_id}/complete", headers=seller).status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        codes = list(pool.map(attempt, range(n)))

    assert codes.count(200) == 1, codes
    assert len(api.store.transactions) == 1


# ---------- Admin-input validation ----------


def test_unknown_adjuster_in_pipeline_rejected(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    """H3: a pipeline referencing a non-existent adjuster is rejected at config time."""
    r = client.put(
        f"/admin/config/pipelines/{basic_service}",
        json={"buyer": ["does_not_exist"], "seller": []},
        headers=admin,
    )
    assert r.status_code == 422


def test_margin_floor_rejects_nan_negative_and_zero_ceiling(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    """H5: negative / out-of-range / non-positive-ceiling margin floors are rejected."""
    for body in (
        {"absolute": -5.0},
        {"pct": -0.1},
        {"pct": 1.5},
        {"ceiling_multiplier": 0.0},
    ):
        r = client.put("/admin/config/margin_floor", json=body, headers=admin)
        assert r.status_code == 422, body

    # NaN/inf can't be sent as compliant JSON; assert the model guard directly.
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            MarginFloorBody(absolute=bad)


def test_service_type_rejects_inf_and_nonpositive_prices(client: TestClient, admin: Header) -> None:
    """M1: inf / non-positive base prices are rejected (inf > 0 is True, so needs allow_inf_nan)."""
    for body in (
        {"base_buyer_price": 0.0, "base_seller_payout": 10.0},
        {"base_buyer_price": 20.0, "base_seller_payout": -1.0},
    ):
        r = client.put("/admin/config/service_types/x", json=body, headers=admin)
        assert r.status_code == 422, body

    with pytest.raises(ValidationError):
        ServiceTypeBody(base_buyer_price=float("inf"), base_seller_payout=10.0)


def test_healthz_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
