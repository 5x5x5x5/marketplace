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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketplace import api, repo
from marketplace.db import SessionLocal
from marketplace.entities import Job, Payment, Payout
from marketplace.models import EventKind, JobStatus, PayoutStatus
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import PaymentEvent
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail
from tests.webhook_helpers import (
    completed_job,
    defer,
    notification_count,
    payout_for,
    post_event,
    replay,
    retry_now,
)

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


# ---------- A2: a full dispute clawback reads as a failed transfer ----------


def _dispute_id(client: TestClient, auth: AuthFactory, job_id: str) -> str:
    r = client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "not done"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _resolve(client: TestClient, admin: Header, dispute_id: str, refund: str, clawback: str) -> int:
    return client.post(
        f"/v1/admin/disputes/{dispute_id}/resolve",
        json={"refund_amount": refund, "clawback_amount": clawback, "note": "arbitrated"},
        headers=admin,
    ).status_code


def _transfer_count(fake: FakeProvider) -> int:
    return len(fake.transfer_keys)


def test_full_clawback_reversal_event_does_not_fail_the_payout(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """A2. Resolving with clawback == seller_payout fully reverses the transfer.
    Stripe reports that as `transfer.reversed` with `reversed=true`, which the
    adapter maps to `transfer_failed` (pinned by
    test_stripe_provider::test_full_transfer_reversal_maps_to_transfer_failed).
    Pre-fix the handler marked the payout FAILED and paged admins, and the
    admin payout retry then paid the seller the full payout again, under a
    fresh key, on top of the clawback."""
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    assert _resolve(client, admin, _dispute_id(client, auth, job_id), "20.00", "14.00") == 200
    assert fake_payments.reversals[-1][:2] == (tr, "14.00")

    post_event(client, event_id="evt_full_reversal", kind="transfer_failed", object_id=tr)

    payout = payout_for(job_id)
    assert payout.status is PayoutStatus.PAID  # the seller was paid, then clawed back
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 0
    transfers_before = _transfer_count(fake_payments)
    r = client.post(f"/v1/admin/payouts/{payout.id}/retry", headers=admin)
    assert r.status_code == 409
    assert _transfer_count(fake_payments) == transfers_before  # no second payout


def test_full_clawback_reversal_event_twice_and_late(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2, replay-safety of the new marker: applied twice, and applied late by
    the retry job, the full-clawback reversal still leaves the payout PAID."""
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    assert _resolve(client, admin, _dispute_id(client, auth, job_id), "0.00", "14.00") == 200

    defer(client, monkeypatch, event_id="evt_rev_late", kind="transfer_failed", object_id=tr)
    retry_now()
    replay(PaymentEvent(event_id="evt_rev_again", kind="transfer_failed", object_id=tr))

    assert payout_for(job_id).status is PayoutStatus.PAID
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 0


def test_partial_clawback_does_not_mask_a_real_transfer_failure(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A2's marker is narrow: only a pinned clawback of the WHOLE payout
    explains a full reversal. With a partial clawback pinned, a full reversal
    is someone else's doing and still marks the payout FAILED."""
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    assert _resolve(client, admin, _dispute_id(client, auth, job_id), "0.00", "4.00") == 200

    post_event(client, event_id="evt_other_reversal", kind="transfer_failed", object_id=tr)

    assert payout_for(job_id).status is PayoutStatus.FAILED
    assert notification_count(EventKind.PAYOUT_FAILED_ADMIN) == 1


def test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """A2, class 3 consequence. The refund leg succeeds and the reversal leg
    errors client-side (a timeout) although the provider executed it, so the
    resolve 502s. The full-reversal event arrives before the admin retries.
    Pre-fix the payout was FAILED by then, the retry's guard answered a 409
    "no paid transfer to claw back" (replayable, forever), and the dispute
    stayed OPEN with the buyer refunded and no REFUND row in the ledger."""
    job_id, _pid = completed_job(client, auth, basic_service)
    tr = payout_for(job_id).provider_transfer_id
    assert tr is not None
    dispute_id = _dispute_id(client, auth, job_id)

    fake_payments.fail_keys = {f"reversal:{job_id}:dispute"}
    assert _resolve(client, admin, dispute_id, "6.00", "14.00") == 502
    assert len(fake_payments.refund_keys) == 1  # money moved to the buyer
    post_event(client, event_id="evt_rev_mid", kind="transfer_failed", object_id=tr)

    assert _resolve(client, admin, dispute_id, "6.00", "14.00") == 200
    with SessionLocal() as s:
        payout = s.scalar(select(Payout).where(Payout.job_id == UUID(job_id)))
        assert payout is not None and payout.status is PayoutStatus.PAID
        assert (
            s.scalar(select(func.count()).select_from(Payout).where(Payout.job_id == UUID(job_id)))
            == 1
        )
