"""Notification preferences: per-kind mutes, money-only must-send floor.
Spec: 2026-07-14-notification-preferences-design.md."""

from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Notification, NotificationMute
from marketplace.models import EventKind, UserRole
from marketplace.notifications import enqueue, enqueue_admins
from tests.conftest import AuthFactory


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


def test_enqueue_admins_filters_only_muted_admin(auth: AuthFactory) -> None:
    auth("admin", "adm1")
    auth("admin", "adm2")
    _mute("adm1", EventKind.REPORT_OPENED_ADMIN)
    with SessionLocal() as s:
        enqueue_admins(
            s,
            EventKind.REPORT_OPENED_ADMIN,
            {
                "report_id": "r",
                "target_kind": "user",
                "target_id": "x",
                "reason": "spam",
                "reporter_id": "y",
            },
        )
        s.commit()
    assert _outbox_kinds("adm1") == []
    assert _outbox_kinds("adm2") == ["report_opened_admin"]
