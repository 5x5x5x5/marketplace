"""Retention sweeps: bounded tables, immortal PENDING outbox rows."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import IdempotencyRecord, Notification, WebhookEvent
from marketplace.models import EventKind, NotificationStatus
from tests.conftest import AuthFactory


def _days_ago(n: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)


def _seed(
    session: Session,
    *,
    idem_age: float,
    hook_age: float,
    note_age: float,
    note_status: NotificationStatus,
) -> None:
    session.add(
        IdempotencyRecord(
            principal="buyer:alice",
            key=f"k-{idem_age}",
            path="/v1/jobs",
            response_status=200,
            response_body="{}",
            created_at=_days_ago(idem_age),
        )
    )
    session.add(
        WebhookEvent(
            provider_event_id=f"evt-{hook_age}",
            kind="payment_succeeded",
            received_at=_days_ago(hook_age),
        )
    )
    session.add(
        Notification(
            user_id="alice",
            email="a@example.com",
            kind=EventKind.JOB_ACCEPTED_BUYER,
            status=note_status,
            created_at=_days_ago(note_age),
        )
    )


def test_retention_reaps_old_keeps_young(client: TestClient, auth: AuthFactory) -> None:
    auth("buyer", "alice")  # FK target for notifications
    with SessionLocal() as s:
        _seed(s, idem_age=8, hook_age=31, note_age=31, note_status=NotificationStatus.SENT)
        _seed(s, idem_age=6, hook_age=29, note_age=29, note_status=NotificationStatus.SENT)
        s.commit()
    with SessionLocal() as s:
        api._sweep_retention(s)  # pyright: ignore[reportPrivateUsage]
        s.commit()
    with SessionLocal() as s:
        assert len(s.scalars(select(IdempotencyRecord)).all()) == 1
        assert len(s.scalars(select(WebhookEvent)).all()) == 1
        assert len(s.scalars(select(Notification)).all()) == 1


def test_pending_outbox_rows_are_immortal(client: TestClient, auth: AuthFactory) -> None:
    auth("buyer", "alice")
    with SessionLocal() as s:
        _seed(s, idem_age=1, hook_age=1, note_age=400, note_status=NotificationStatus.PENDING)
        _seed(s, idem_age=1.5, hook_age=1.5, note_age=400, note_status=NotificationStatus.FAILED)
        s.commit()
    with SessionLocal() as s:
        api._sweep_retention(s)  # pyright: ignore[reportPrivateUsage]
        s.commit()
    with SessionLocal() as s:
        kept = s.scalars(select(Notification)).all()
        assert [n.status for n in kept] == [NotificationStatus.PENDING]


def test_retention_sweep_is_idempotent(client: TestClient, auth: AuthFactory) -> None:
    auth("buyer", "alice")
    with SessionLocal() as s:
        _seed(s, idem_age=8, hook_age=31, note_age=31, note_status=NotificationStatus.FAILED)
        s.commit()
    for _ in range(2):
        with SessionLocal() as s:
            api._sweep_retention(s)  # pyright: ignore[reportPrivateUsage]
            s.commit()
    with SessionLocal() as s:
        assert s.scalars(select(IdempotencyRecord)).all() == []
