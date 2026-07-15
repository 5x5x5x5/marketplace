"""Notification preferences: per-kind mutes, money-only must-send floor.
Spec: 2026-07-14-notification-preferences-design.md."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Notification, NotificationMute
from marketplace.models import EventKind, UserRole
from marketplace.notifications import enqueue, enqueue_admins
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail

_REPORT_PAYLOAD = {
    "report_id": "r",
    "target_kind": "user",
    "target_id": "x",
    "reason": "spam",
    "reporter_id": "y",
}


def test_mutes_table_registered() -> None:
    from marketplace.entities import Base

    assert "notification_mutes" in Base.metadata.tables


def test_kind_roles_covers_every_kind() -> None:
    """A future kind added without a role mapping must fail fast (same
    pattern as the every-kind-has-a-renderer invariant)."""
    from marketplace.notifications import KIND_ROLES

    assert set(KIND_ROLES) == set(EventKind)
    assert set(KIND_ROLES.values()) <= {UserRole.BUYER, UserRole.SELLER, UserRole.ADMIN}


def test_must_send_is_the_money_floor() -> None:
    from marketplace.notifications import KIND_ROLES, MUST_SEND

    assert {
        EventKind.REFUND_ISSUED_BUYER,
        EventKind.DISPUTE_RESOLVED_BUYER,
        EventKind.DISPUTE_RESOLVED_SELLER,
        EventKind.PAYOUT_FAILED_ADMIN,
    } == MUST_SEND
    assert set(KIND_ROLES) >= MUST_SEND


def _mute(user_id: str, kind: EventKind) -> None:
    with SessionLocal() as s:
        s.add(NotificationMute(user_id=user_id, kind=kind))
        s.commit()


def _outbox_kinds(user_id: str) -> list[str]:
    with SessionLocal() as s:
        rows = s.scalars(select(Notification).where(Notification.user_id == user_id)).all()
        return [str(r.kind) for r in rows]


def test_enqueue_skips_muted_kind(auth: AuthFactory) -> None:
    auth("seller", "s1")
    _mute("s1", EventKind.OFFER_RECEIVED)
    with SessionLocal() as s:
        enqueue(s, EventKind.OFFER_RECEIVED, "s1", {"job_id": "j"})
        s.commit()
    assert _outbox_kinds("s1") == []


def test_enqueue_ignores_smuggled_mute_on_money_kind(auth: AuthFactory) -> None:
    """The floor is server-side: even a directly-inserted mute row for a
    money kind must not suppress the mail."""
    auth("buyer", "alice")
    _mute("alice", EventKind.REFUND_ISSUED_BUYER)
    with SessionLocal() as s:
        enqueue(s, EventKind.REFUND_ISSUED_BUYER, "alice", {"job_id": "j", "buyer_price": "1.00"})
        s.commit()
    assert _outbox_kinds("alice") == ["refund_issued_buyer"]


def test_enqueue_admins_filters_only_muted_admin(
    auth: AuthFactory, caplog: pytest.LogCaptureFixture
) -> None:
    auth("admin", "adm1")
    auth("admin", "adm2")
    _mute("adm1", EventKind.REPORT_OPENED_ADMIN)
    with caplog.at_level(logging.WARNING, logger="marketplace.notifications"), SessionLocal() as s:
        enqueue_admins(s, EventKind.REPORT_OPENED_ADMIN, _REPORT_PAYLOAD)
        s.commit()
    assert _outbox_kinds("adm1") == []
    assert _outbox_kinds("adm2") == ["report_opened_admin"]
    # adm2 still received it -> no zero-recipient warning
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_enqueue_admins_warns_when_all_recipients_muted(
    auth: AuthFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Admins exist, but every one of them muted this kind: the fan-out sends
    nothing, and that silent zero-recipient case must be logged (unlike the
    no-admin-accounts branch, which already warns on its own)."""
    auth("admin", "adm1")
    auth("admin", "adm2")
    _mute("adm1", EventKind.REPORT_OPENED_ADMIN)
    _mute("adm2", EventKind.REPORT_OPENED_ADMIN)
    with caplog.at_level(logging.WARNING, logger="marketplace.notifications"), SessionLocal() as s:
        enqueue_admins(s, EventKind.REPORT_OPENED_ADMIN, _REPORT_PAYLOAD)
        s.commit()
    assert _outbox_kinds("adm1") == []
    assert _outbox_kinds("adm2") == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "report_opened_admin" in warnings[0].getMessage()


def test_get_defaults_and_role_scoping(
    client: TestClient, auth: AuthFactory, admin: Header
) -> None:
    buyer_rows = client.get("/v1/notification-preferences", headers=auth("buyer", "alice")).json()
    assert {r["kind"] for r in buyer_rows} == {
        "job_accepted_buyer",
        "job_completed_buyer",
        "job_expired_buyer",
        "refund_issued_buyer",
        "dispute_resolved_buyer",
    }
    assert all(r["muted"] is False for r in buyer_rows)
    locked = {r["kind"] for r in buyer_rows if r["locked"]}
    assert locked == {"refund_issued_buyer", "dispute_resolved_buyer"}

    seller_rows = client.get("/v1/notification-preferences", headers=auth("seller", "s1")).json()
    assert len(seller_rows) == 4
    admin_rows = client.get("/v1/notification-preferences", headers=admin).json()
    assert len(admin_rows) == 5


def test_put_replace_set_and_end_to_end_mute(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    seller = auth("seller", "s1")

    r = client.put(
        "/v1/notification-preferences", json={"muted": ["offer_received"]}, headers=seller
    )
    assert r.status_code == 200
    assert {x["kind"]: x["muted"] for x in r.json()}["offer_received"] is True

    new_job(client, auth, basic_service, "alice")  # matches s1, offer created
    assert _outbox_kinds("s1") == []  # nudge suppressed
    # in-app unaffected: the offer row exists
    assert len(client.get("/v1/seller/offers", headers=seller).json()) == 1

    # replace-set: PUT a different set unmutes offer_received
    r = client.put(
        "/v1/notification-preferences", json={"muted": ["job_cancelled_seller"]}, headers=seller
    )
    assert r.status_code == 200
    new_job(client, auth, basic_service, "alice")
    assert "offer_received" in _outbox_kinds("s1")


def test_put_rejects_must_send_off_role_and_unknown(client: TestClient, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    for bad in (["refund_issued_buyer"], ["offer_received"], ["not_a_kind"]):
        r = client.put("/v1/notification-preferences", json={"muted": bad}, headers=buyer)
        assert r.status_code == 422, bad
    # and the failed PUTs changed nothing
    rows = client.get("/v1/notification-preferences", headers=buyer).json()
    assert all(x["muted"] is False for x in rows)


def test_put_is_idempotent_and_tolerates_duplicates(client: TestClient, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    body = {"muted": ["job_accepted_buyer", "job_accepted_buyer"]}
    assert client.put("/v1/notification-preferences", json=body, headers=buyer).status_code == 200
    assert client.put("/v1/notification-preferences", json=body, headers=buyer).status_code == 200
    with SessionLocal() as s:
        rows = s.scalars(select(NotificationMute).where(NotificationMute.user_id == "alice")).all()
        assert len(rows) == 1


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_puts_last_writer_wins(client: TestClient, auth: AuthFactory) -> None:
    """Naive delete-then-insert unions concurrent sets; the FOR UPDATE user
    lock serializes them so the result is exactly one caller's set."""
    buyer = auth("buyer", "alice")
    set_a = ["job_accepted_buyer"]
    set_b = ["job_completed_buyer", "job_expired_buyer"]
    barrier = threading.Barrier(2)

    def put(muted: list[str]) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.put(
            "/v1/notification-preferences", json={"muted": muted}, headers=buyer
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(put, [set_a, set_b]))
    assert all(code in (200, 409) for code in codes), codes
    with SessionLocal() as s:
        kinds = sorted(
            str(m.kind)
            for m in s.scalars(
                select(NotificationMute).where(NotificationMute.user_id == "alice")
            ).all()
        )
    assert kinds in (sorted(set_a), sorted(set_b)), kinds


def test_suspended_user_not_gated_from_preferences(
    client: TestClient, auth: AuthFactory, admin: Header
) -> None:
    """Spec commitment: notification preferences are NOT suspension-gated
    (unlike acquisition/action verbs — see test_moderation.py's suspended-verb
    tests). A suspended user must still be able to read and change their mutes."""
    buyer = auth("buyer", "alice")
    r = client.post("/v1/admin/users/alice/suspend", json={"reason": "abuse"}, headers=admin)
    assert r.status_code == 200, r.text

    r = client.get("/v1/notification-preferences", headers=buyer)
    assert r.status_code == 200
    assert all(x["muted"] is False for x in r.json())

    r = client.put(
        "/v1/notification-preferences", json={"muted": ["job_accepted_buyer"]}, headers=buyer
    )
    assert r.status_code == 200
    assert {x["kind"]: x["muted"] for x in r.json()}["job_accepted_buyer"] is True

    r = client.get("/v1/notification-preferences", headers=buyer)
    assert r.status_code == 200
    assert {x["kind"]: x["muted"] for x in r.json()}["job_accepted_buyer"] is True
