"""Money-path audit regressions (docs/money-path-audit-2026-11.md).

Each test names the finding it pins. Everything goes through the HTTP API
(or the webhook helpers), except the race tests' one widening seam: they
slow `repo.active_job_count`, which `accept_offer` calls while it already
holds the offer and seller-profile locks but before it locks the job."""

import threading
import time
from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace import api, repo
from marketplace.db import SessionLocal
from marketplace.entities import Job, Payment
from marketplace.models import JobStatus
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail

# ---------- A1: cancel locks Job then Offer, accept/decline/sweep lock Offer then Job ----------


def _slow_active_job_count(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    real: Callable[[Session, str], int] = repo.active_job_count

    def slow(session: Session, seller_id: str) -> int:
        n = real(session, seller_id)
        time.sleep(seconds)
        return n

    monkeypatch.setattr(repo, "active_job_count", slow)


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
@pytest.mark.parametrize("canceller", ["buyer", "admin"])
def test_cancel_vs_accept_never_deadlocks(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
    canceller: str,
) -> None:
    """A1. Accept holds the offer lock and waits for the job; cancel held the
    job and then wrote the offer (expiring it). Postgres broke the cycle by
    killing one side with a deadlock error, a 500. Cancel must win cleanly
    (200) and accept must lose cleanly (409), with nothing charged."""
    onboard_and_avail(client, auth, basic_service, "s1")
    seller = auth("seller", "s1")
    job_id = str(new_job(client, auth, basic_service, "alice")["id"])
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]
    _slow_active_job_count(monkeypatch, 0.6)

    codes: dict[str, int] = {}

    def do_accept() -> None:
        c = TestClient(api.app, raise_server_exceptions=False)
        codes["accept"] = c.post(
            f"/v1/seller/offers/{offer['id']}/accept", headers=seller
        ).status_code

    def do_cancel() -> None:
        time.sleep(0.2)  # accept now holds the offer + seller locks, not yet the job
        c = TestClient(api.app, raise_server_exceptions=False)
        if canceller == "buyer":
            r = c.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
        else:
            r = c.post(f"/v1/admin/jobs/{job_id}/cancel", headers=admin)
        codes["cancel"] = r.status_code

    threads = [threading.Thread(target=do_accept), threading.Thread(target=do_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes == {"accept": 409, "cancel": 200}, codes
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        assert job is not None and job.status == JobStatus.CANCELLED
        assert s.scalar(select(Payment).where(Payment.job_id == UUID(job_id))) is None
