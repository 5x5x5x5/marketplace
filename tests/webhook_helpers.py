"""Shared helpers for the webhook outbox and replay-safety tests."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Notification, Payment, Payout, WebhookEvent
from marketplace.models import EventKind, WebhookEventStatus
from marketplace.payments.port import PaymentEvent
from tests.conftest import AuthFactory
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail

ApplyFn = Callable[[Session, PaymentEvent], None]


def fail_events(monkeypatch: pytest.MonkeyPatch, *event_ids: str) -> set[str]:
    """Make the handler raise for the given event ids (and only those). The
    returned set is live: discard an id to let its retries succeed."""
    failing = set(event_ids)
    real: ApplyFn = api._apply_payment_event  # pyright: ignore[reportPrivateUsage]

    def flaky(session: Session, event: PaymentEvent) -> None:
        if event.event_id in failing:
            raise RuntimeError(f"simulated handler failure for {event.event_id}")
        real(session, event)

    monkeypatch.setattr(api, "_apply_payment_event", flaky)
    return failing


def post_event(client: TestClient, **fields: Any) -> dict[str, str]:
    r = client.post("/v1/payments/webhook", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def defer(client: TestClient, monkeypatch: pytest.MonkeyPatch, **fields: Any) -> None:
    """Deliver an event whose first apply fails, leaving it FAILED for the
    retry job; the handler is healthy again afterwards."""
    failing = fail_events(monkeypatch, str(fields["event_id"]))
    assert post_event(client, **fields) == {"status": "deferred"}
    failing.clear()


def make_due() -> None:
    with SessionLocal() as s:
        for row in s.scalars(
            select(WebhookEvent).where(WebhookEvent.status == WebhookEventStatus.FAILED)
        ).all():
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()


def retry_now() -> None:
    """Run the retry job with every FAILED row due."""
    make_due()
    api._run_webhook_retry_once()  # pyright: ignore[reportPrivateUsage]


def replay(event: PaymentEvent) -> None:
    """Apply an event straight through the handler, as the retry job does."""
    with SessionLocal() as s:
        api._apply_payment_event(s, event)  # pyright: ignore[reportPrivateUsage]
        s.commit()


def row(event_id: str) -> WebhookEvent:
    with SessionLocal() as s:
        found = s.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id == event_id))
        assert found is not None
        return found


def notification_count(kind: EventKind) -> int:
    with SessionLocal() as s:
        return (
            s.scalar(
                select(func.count()).select_from(Notification).where(Notification.kind == kind)
            )
            or 0
        )


def completed_job(client: TestClient, auth: AuthFactory, sid: str) -> tuple[str, str]:
    """A paid, completed job; returns (job_id, provider_payment_id)."""
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )
    assert pid is not None
    return str(job["id"]), pid


def payout_for(job_id: str) -> Payout:
    with SessionLocal() as s:
        payout = s.scalar(select(Payout).where(Payout.job_id == UUID(job_id)))
        assert payout is not None
        return payout
