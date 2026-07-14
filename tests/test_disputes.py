"""Disputes, arbitration, adjustments ledger, chargebacks."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from marketplace.db import SessionLocal
from marketplace.entities import Job
from marketplace.mail import RecordingEmailSender
from marketplace.notifications import drain_once
from tests.conftest import AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def test_dispute_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"disputes", "adjustments"} <= set(Base.metadata.tables)


def _drain() -> RecordingEmailSender:
    recorder = RecordingEmailSender()
    drain_once(recorder)
    return recorder


def _completed_job(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    return str(job["id"])


def _open_dispute(client: TestClient, auth: AuthFactory, job_id: str) -> dict[str, object]:
    r = client.post(
        f"/v1/jobs/{job_id}/dispute",
        json={"reason": "work was not as described"},
        headers=auth("buyer", "alice"),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_open_dispute_happy_path_and_views(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _drain()  # clear lifecycle mail
    body = _open_dispute(client, auth, job_id)
    assert body["status"] == "open"
    assert "clawback_amount" not in body  # buyer never sees seller money

    seller_view = client.get(
        f"/v1/seller/jobs/{job_id}/dispute", headers=auth("seller", "s1")
    ).json()
    assert seller_view["reason"] == "work was not as described"
    assert "refund_amount" not in seller_view  # seller never sees buyer money

    queue = client.get("/v1/admin/disputes", headers=admin).json()
    assert len(queue) == 1 and queue[0]["source"] == "buyer"

    recorder = _drain()
    seller_mail = [m for m in recorder.sent if "s1@" in m[0]]
    admin_mail = [m for m in recorder.sent if "ops@" in m[0]]
    assert seller_mail and "not as described" in seller_mail[0][2]
    assert admin_mail  # arbitration ping


def test_open_dispute_guards(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    # Not completed yet -> 409.
    r = client.post(
        f"/v1/jobs/{job['id']}/dispute", json={"reason": "x"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409
    # Someone else's job -> 404.
    r = client.post(
        f"/v1/jobs/{job['id']}/dispute", json={"reason": "x"}, headers=auth("buyer", "bob")
    )
    assert r.status_code == 404


def test_dispute_window_and_duplicate_guards(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    _open_dispute(client, auth, job_id)
    r = client.post(
        f"/v1/jobs/{job_id}/dispute", json={"reason": "again"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409  # one dispute per job

    job2_id = _completed_job(client, auth, basic_service)
    with SessionLocal() as s:
        job2 = s.get(Job, UUID(job2_id))
        assert job2 is not None
        job2.completed_at = datetime.now(UTC) - timedelta(days=8)
        s.commit()
    r = client.post(
        f"/v1/jobs/{job2_id}/dispute", json={"reason": "late"}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 409  # window elapsed
