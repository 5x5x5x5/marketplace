"""Admin stats endpoint: the operator's one-call snapshot."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail


def test_stats_empty_db_full_enum_keys(client: TestClient, admin: Header) -> None:
    s = client.get("/v1/admin/stats", headers=admin).json()
    assert s["jobs"] == {
        "pending": 0,
        "awaiting_payment": 0,
        "accepted": 0,
        "completed": 0,
        "expired": 0,
        "cancelled": 0,
    }
    assert set(s["payments"]) == {"pending", "succeeded", "failed", "refunded"}
    assert set(s["payouts"]) == {"pending", "paid", "failed"}
    assert s["notifications"]["pending"] == 0
    assert s["notifications"]["oldest_pending_age_seconds"] is None
    assert s["disputes_open"] == 0 and s["reports_open"] == 0
    assert s["quotes_live"] == 0
    assert s["uptime_seconds"] >= 0
    assert set(s["retention"]) == {"idempotency_keys", "webhook_events", "notifications_total"}


def test_stats_counts_a_real_flow(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))

    s = client.get("/v1/admin/stats", headers=admin).json()
    assert s["jobs"]["completed"] == 1
    assert s["payments"]["succeeded"] == 1
    assert s["payouts"]["paid"] == 1
    assert s["notifications"]["pending"] > 0  # outbox not drained in tests
    assert s["notifications"]["oldest_pending_age_seconds"] is not None
    assert s["notifications"]["oldest_pending_age_seconds"] >= 0
    assert s["users"]["buyer"] >= 1 and s["users"]["seller"] >= 1
    assert s["users"]["suspended"] == 0


def test_stats_admin_only(client: TestClient, auth: AuthFactory) -> None:
    assert client.get("/v1/admin/stats", headers=auth("buyer", "alice")).status_code == 403


def test_stats_counts_suspension(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("buyer", "alice")  # materialize the user row
    r = client.post("/v1/admin/users/alice/suspend", json={"reason": "abuse"}, headers=admin)
    assert r.status_code == 200, r.text
    s = client.get("/v1/admin/stats", headers=admin).json()
    assert s["users"]["suspended"] == 1
