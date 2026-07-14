"""Transactional-outbox notifications: enqueue, renderers, drain, emitters."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Notification, User
from marketplace.mail import RecordingEmailSender
from marketplace.models import EventKind, NotificationStatus, UserRole
from marketplace.notifications import RENDERERS, drain_once, enqueue, enqueue_admins


def test_notifications_table_registered() -> None:
    from marketplace.entities import Base

    assert "notifications" in Base.metadata.tables


def _make_user(user_id: str, role: UserRole = UserRole.SELLER) -> None:
    with SessionLocal() as s:
        s.add(
            User(
                id=user_id,
                email=f"{user_id}@x.test.local",
                role=role,
                password_hash="irrelevant",
                display_name=user_id,
            )
        )
        s.commit()


def _enqueue_offer(user_id: str = "s1") -> None:
    with SessionLocal() as s:
        enqueue(
            s,
            EventKind.OFFER_RECEIVED,
            user_id,
            {
                "job_id": "j-1",
                "service_type_id": "detailing",
                "seller_payout": "14.00",
                "expires_at": "2026-07-14T12:00:00+00:00",
            },
        )
        s.commit()


def test_enqueue_snapshots_recipient_email() -> None:
    _make_user("s1")
    _enqueue_offer()
    with SessionLocal() as s:
        row = s.scalar(select(Notification))
        assert row is not None
        assert row.email == "s1@x.test.local"
        assert row.status is NotificationStatus.PENDING
        assert row.kind is EventKind.OFFER_RECEIVED


def test_enqueue_missing_user_is_a_logged_skip() -> None:
    with SessionLocal() as s:
        enqueue(s, EventKind.OFFER_RECEIVED, "ghost", {"job_id": "j"})
        s.commit()
    with SessionLocal() as s:
        assert s.scalar(select(Notification)) is None  # skipped, not crashed


def test_drain_sends_and_marks_sent() -> None:
    _make_user("s1")
    _enqueue_offer()
    recorder = RecordingEmailSender()
    assert drain_once(recorder) == 1
    assert recorder.sent[0][0] == "s1@x.test.local"
    assert "14.00" in recorder.sent[0][2]
    with SessionLocal() as s:
        row = s.scalar(select(Notification))
        assert row is not None and row.status is NotificationStatus.SENT
        assert row.sent_at is not None
    # Replay safety: nothing left to send.
    assert drain_once(RecordingEmailSender()) == 0


class _ExplodingSender:
    def send(self, to: str, subject: str, body: str) -> None:
        raise RuntimeError("provider down")


def test_drain_failure_backs_off_then_terminal() -> None:
    _make_user("s1")
    _enqueue_offer()
    assert drain_once(_ExplodingSender()) == 0
    with SessionLocal() as s:
        row = s.scalar(select(Notification))
        assert row is not None
        assert row.attempts == 1
        assert row.status is NotificationStatus.PENDING
        assert row.next_attempt_at > datetime.now(UTC)  # backed off
        assert row.last_error is not None and "provider down" in row.last_error
        # Fast-forward to the terminal attempt (max is 5).
        row.attempts = 4
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert drain_once(_ExplodingSender()) == 0
    with SessionLocal() as s:
        row = s.scalar(select(Notification))
        assert row is not None and row.status is NotificationStatus.FAILED


def test_backed_off_rows_are_not_due() -> None:
    _make_user("s1")
    _enqueue_offer()
    drain_once(_ExplodingSender())  # attempt 1, next_attempt_at in the future
    assert drain_once(RecordingEmailSender()) == 0  # not due yet


def test_enqueue_admins_fans_out_and_skips_when_none() -> None:
    payload: dict[str, Any] = {"job_id": "j", "payout_id": "p", "seller_id": "s", "amount": "14.00"}
    with SessionLocal() as s:
        enqueue_admins(s, EventKind.PAYOUT_FAILED_ADMIN, payload)  # no admins yet
        s.commit()
    with SessionLocal() as s:
        assert s.scalar(select(Notification)) is None
    _make_user("ops1", UserRole.ADMIN)
    _make_user("ops2", UserRole.ADMIN)
    with SessionLocal() as s:
        enqueue_admins(s, EventKind.PAYOUT_FAILED_ADMIN, payload)
        s.commit()
    recorder = RecordingEmailSender()
    assert drain_once(recorder) == 2
    assert {m[0] for m in recorder.sent} == {"ops1@x.test.local", "ops2@x.test.local"}


def test_every_kind_has_a_renderer() -> None:
    assert set(RENDERERS) == set(EventKind)
