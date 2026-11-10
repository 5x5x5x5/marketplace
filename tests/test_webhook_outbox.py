"""Webhook outbox: a handler failure is persisted FAILED and answered
`deferred`; any existing row makes a redelivery a `duplicate`; the retry job
re-applies with backoff and dead-letters at the cap; concurrent first
deliveries never 500; retention spares FAILED rows; operators can list and
re-queue rows."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import (
    AuditLog,
    Job,
    Notification,
    Payment,
    SellerProfile,
    WebhookEvent,
)
from marketplace.mail import RecordingEmailSender
from marketplace.models import EventKind, JobStatus, PaymentStatus, WebhookEventStatus
from marketplace.notifications import drain_once
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import PaymentEvent
from marketplace.settings import settings
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import pending_accept
from tests.webhook_helpers import (
    defer,
    fail_events,
    notification_count,
    post_event,
    retry_now,
    row,
)


def _job_state(job_id: str) -> tuple[JobStatus, PaymentStatus]:
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job_id)))
        assert job is not None and payment is not None
        return job.status, payment.status


# ---------- first-delivery failure ----------


def test_handler_failure_persists_failed_row_and_defers(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    fail_events(monkeypatch, "evt_fail_1")
    before = datetime.now(UTC)

    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_fail_1", "kind": "payment_succeeded", "object_id": pid},
        headers={"stripe-signature": "t=1,v1=not-stored"},
    )
    assert r.status_code == 200  # never a 500: that would only invite redelivery storms
    assert r.json() == {"status": "deferred"}

    failed = row("evt_fail_1")
    assert failed.status is WebhookEventStatus.FAILED
    assert failed.attempts == 1
    assert failed.object_id == pid
    assert failed.last_error is not None and "simulated handler failure" in failed.last_error
    assert failed.next_attempt_at is not None
    base = timedelta(seconds=settings.webhook_retry_base_seconds)
    assert before + base <= failed.next_attempt_at <= datetime.now(UTC) + base
    # Only the parsed event fields: never the raw body or the signature.
    assert failed.payload == {
        "event_id": "evt_fail_1",
        "kind": "payment_succeeded",
        "object_id": pid,
        "payments_ready": None,
        "amount_minor": None,
        "outcome": None,
        "related_id": None,
    }
    assert "not-stored" not in str(failed.payload)

    # The failed apply rolled back whole: no half-transition.
    assert _job_state(job_id) == (JobStatus.AWAITING_PAYMENT, PaymentStatus.PENDING)


def test_last_error_keeps_only_the_first_line(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driver errors carry the SQL and bound parameters on later lines; those
    belong in the server log, not on the row or in admin mail."""

    def noisy(_s: Session, _e: PaymentEvent) -> None:
        raise RuntimeError("constraint violated\n[SQL: INSERT ...]\n[parameters: secret-ish]")

    monkeypatch.setattr(api, "_apply_payment_event", noisy)
    assert post_event(client, event_id="evt_noisy", kind="ignored", object_id="x") == {
        "status": "deferred"
    }
    assert row("evt_noisy").last_error == "RuntimeError: constraint violated"


@pytest.mark.parametrize("status", [WebhookEventStatus.FAILED, WebhookEventStatus.DEAD_LETTER])
def test_redelivery_of_unapplied_event_is_duplicate(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    status: WebhookEventStatus,
) -> None:
    """Any existing row means seen: recovery belongs to the retry job (or an
    operator), never to a redelivery racing it."""
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    with SessionLocal() as s:
        s.add(
            WebhookEvent(
                provider_event_id="evt_redeliver",
                kind="payment_succeeded",
                object_id=pid,
                status=status,
                attempts=1,
                next_attempt_at=datetime.now(UTC) + timedelta(minutes=1),
                payload={
                    "event_id": "evt_redeliver",
                    "kind": "payment_succeeded",
                    "object_id": pid,
                },
            )
        )
        s.commit()
    assert post_event(
        client, event_id="evt_redeliver", kind="payment_succeeded", object_id=pid
    ) == {"status": "duplicate"}
    assert _job_state(job_id) == (JobStatus.AWAITING_PAYMENT, PaymentStatus.PENDING)


# ---------- retry job ----------


def test_retry_job_applies_when_blocker_clears(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    defer(client, monkeypatch, event_id="evt_retry_ok", kind="payment_succeeded", object_id=pid)

    retry_now()

    applied = row("evt_retry_ok")
    assert applied.status is WebhookEventStatus.APPLIED
    assert applied.next_attempt_at is None
    assert _job_state(job_id) == (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED)


def test_retry_job_skips_rows_not_yet_due(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    defer(client, monkeypatch, event_id="evt_later", kind="payment_succeeded", object_id=pid)

    api._run_webhook_retry_once()  # pyright: ignore[reportPrivateUsage]  # backoff not elapsed

    assert row("evt_later").status is WebhookEventStatus.FAILED
    assert _job_state(job_id) == (JobStatus.AWAITING_PAYMENT, PaymentStatus.PENDING)


def test_retry_job_backs_off_exponentially_while_still_failing(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    fail_events(monkeypatch, "evt_stuck")
    post_event(client, event_id="evt_stuck", kind="payment_succeeded", object_id=pid)

    retry_now()

    stuck = row("evt_stuck")
    assert stuck.status is WebhookEventStatus.FAILED
    assert stuck.attempts == 2
    second_step = timedelta(seconds=settings.webhook_retry_base_seconds * 5)
    assert stuck.next_attempt_at is not None
    assert stuck.next_attempt_at > datetime.now(UTC) + second_step - timedelta(seconds=5)


def test_retry_job_isolates_rows_with_per_row_savepoints(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row still failing must not roll back another row's successful
    re-apply in the same pass."""
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s2"))
    failing = fail_events(monkeypatch, "evt_good", "evt_bad")
    post_event(client, event_id="evt_good", kind="payment_succeeded", object_id=pid)
    post_event(
        client,
        event_id="evt_bad",
        kind="account_updated",
        object_id="acct_fake_s2",
        payments_ready=False,
    )
    failing.discard("evt_good")

    retry_now()

    assert row("evt_good").status is WebhookEventStatus.APPLIED
    assert row("evt_bad").status is WebhookEventStatus.FAILED
    assert _job_state(job_id) == (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED)


def test_retry_job_exhaustion_dead_letters_and_notifies(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    fail_events(monkeypatch, "evt_dead")
    post_event(client, event_id="evt_dead", kind="payment_succeeded", object_id=pid)
    with SessionLocal() as s:
        dead = s.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_dead"))
        assert dead is not None
        dead.attempts = settings.webhook_max_attempts - 1
        s.commit()

    retry_now()

    dead = row("evt_dead")
    assert dead.status is WebhookEventStatus.DEAD_LETTER
    assert dead.attempts == settings.webhook_max_attempts
    assert dead.next_attempt_at is None
    with SessionLocal() as s:
        notice = s.scalar(
            select(Notification).where(Notification.kind == EventKind.WEBHOOK_DEAD_LETTER_ADMIN)
        )
        assert notice is not None
        assert notice.payload["webhook_id"] == str(dead.id)
        assert notice.payload["provider_event_id"] == "evt_dead"
    recorder = RecordingEmailSender()
    drain_once(recorder)
    [(_to, subject, body)] = [m for m in recorder.sent if "DEAD-LETTERED" in m[1]]
    assert "evt_dead" in subject
    assert "GET /v1/admin/webhooks?status=dead_letter" in body
    assert f"POST /v1/admin/webhooks/{dead.id}/retry" in body


def test_dead_letter_at_first_delivery_when_cap_is_one(
    client: TestClient,
    admin: Header,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "webhook_max_attempts", 1)
    fail_events(monkeypatch, "evt_once")
    assert post_event(client, event_id="evt_once", kind="ignored", object_id="x") == {
        "status": "deferred"
    }
    assert row("evt_once").status is WebhookEventStatus.DEAD_LETTER
    assert notification_count(EventKind.WEBHOOK_DEAD_LETTER_ADMIN) == 1


def test_retry_job_dead_letters_a_row_without_payload(admin: Header) -> None:
    with SessionLocal() as s:
        s.add(
            WebhookEvent(
                provider_event_id="evt_nopayload",
                kind="payment_succeeded",
                status=WebhookEventStatus.FAILED,
                attempts=settings.webhook_max_attempts - 1,
                next_attempt_at=datetime.now(UTC),
            )
        )
        s.commit()
    retry_now()
    dead = row("evt_nopayload")
    assert dead.status is WebhookEventStatus.DEAD_LETTER
    assert dead.last_error == "ValueError: no stored payload to replay"


# ---------- races ----------


def test_persist_failed_never_demotes_applied_row() -> None:
    """A late failure path racing a concurrent winner that already applied
    must leave the winner's row alone."""
    with SessionLocal() as s:
        s.add(WebhookEvent(provider_event_id="evt_won", kind="ignored", attempts=0))
        s.commit()
    event = PaymentEvent(event_id="evt_won", kind="ignored", object_id="x")
    api._persist_failed_webhook(event, RuntimeError("lost the race"))  # pyright: ignore[reportPrivateUsage]
    won = row("evt_won")
    assert (won.status, won.attempts, won.last_error) == (WebhookEventStatus.APPLIED, 0, None)


def test_persist_failed_never_touches_dead_letter_row() -> None:
    with SessionLocal() as s:
        s.add(
            WebhookEvent(
                provider_event_id="evt_dl",
                kind="ignored",
                status=WebhookEventStatus.DEAD_LETTER,
                attempts=settings.webhook_max_attempts,
                last_error="original",
            )
        )
        s.commit()
    event = PaymentEvent(event_id="evt_dl", kind="ignored", object_id="x")
    api._persist_failed_webhook(event, RuntimeError("straggler"))  # pyright: ignore[reportPrivateUsage]
    dl = row("evt_dl")
    assert (dl.status, dl.attempts, dl.last_error) == (
        WebhookEventStatus.DEAD_LETTER,
        settings.webhook_max_attempts,
        "original",
    )


def test_persist_failed_advances_existing_failed_row() -> None:
    with SessionLocal() as s:
        s.add(
            WebhookEvent(
                provider_event_id="evt_again",
                kind="ignored",
                status=WebhookEventStatus.FAILED,
                attempts=1,
                next_attempt_at=datetime.now(UTC),
            )
        )
        s.commit()
    event = PaymentEvent(event_id="evt_again", kind="ignored", object_id="x")
    api._persist_failed_webhook(event, RuntimeError("still broken"))  # pyright: ignore[reportPrivateUsage]
    again = row("evt_again")
    assert again.status is WebhookEventStatus.FAILED
    assert again.attempts == 2
    assert again.last_error == "RuntimeError: still broken"


def test_concurrent_first_delivery_loser_answers_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both first deliveries pass the unlocked dedup read; the loser's insert
    then meets the winner's row at the unique constraint and must answer
    `duplicate`, leaving the winner's row untouched."""
    with SessionLocal() as s:
        s.add(WebhookEvent(provider_event_id="evt_race", kind="ignored", attempts=0))
        s.commit()

    def never_seen(_session: Session, _event_id: str) -> bool:
        return False

    monkeypatch.setattr(api, "_webhook_seen", never_seen)

    event = PaymentEvent(event_id="evt_race", kind="ignored", object_id="x")
    assert api._process_webhook(event) == {"status": "duplicate"}  # pyright: ignore[reportPrivateUsage]

    with SessionLocal() as s:
        rows = s.scalars(select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_race"))
        assert [r.status for r in rows] == [WebhookEventStatus.APPLIED]


def test_persist_failed_insert_race_falls_back_to_the_winner_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failing first deliveries both try to create the FAILED row; the
    loser's insert hits the unique constraint, re-selects the winner's row,
    and advances it once instead of raising."""
    real = api._record_webhook_failure  # pyright: ignore[reportPrivateUsage]
    sneaked = False

    def sneak_in_first(session: Session, r: WebhookEvent, exc: Exception) -> None:
        nonlocal sneaked
        if not sneaked:
            sneaked = True
            with SessionLocal() as other:
                other.add(
                    WebhookEvent(
                        provider_event_id="evt_double",
                        kind="ignored",
                        status=WebhookEventStatus.FAILED,
                        attempts=1,
                        next_attempt_at=datetime.now(UTC),
                    )
                )
                other.commit()
        real(session, r, exc)

    monkeypatch.setattr(api, "_record_webhook_failure", sneak_in_first)
    event = PaymentEvent(event_id="evt_double", kind="ignored", object_id="x")
    api._persist_failed_webhook(event, RuntimeError("boom"))  # pyright: ignore[reportPrivateUsage]

    with SessionLocal() as s:
        rows = s.scalars(
            select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_double")
        ).all()
        assert [(r.status, r.attempts) for r in rows] == [(WebhookEventStatus.FAILED, 2)]


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_parallel_first_deliveries_apply_once_and_never_500(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, pid = pending_accept(client, auth, basic_service, fake_payments)
    real = api._apply_payment_event  # pyright: ignore[reportPrivateUsage]
    applies: list[str] = []

    def slow_apply(session: Session, event: PaymentEvent) -> None:
        applies.append(event.event_id)
        time.sleep(0.3)  # hold the winner's uncommitted row while the loser arrives
        real(session, event)

    monkeypatch.setattr(api, "_apply_payment_event", slow_apply)
    barrier = threading.Barrier(2)

    def deliver() -> tuple[int, dict[str, str]]:
        c = TestClient(api.app)
        barrier.wait()
        r = c.post(
            "/v1/payments/webhook",
            json={"event_id": "evt_parallel", "kind": "payment_succeeded", "object_id": pid},
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(deliver), pool.submit(deliver)]]

    assert sorted(body["status"] for _code, body in results) == ["duplicate", "ok"]
    assert all(code == 200 for code, _body in results)
    assert applies == ["evt_parallel"]
    assert _job_state(job_id) == (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED)


# ---------- retention ----------


def test_retention_spares_failed_rows_and_reaps_terminal_ones() -> None:
    old = datetime.now(UTC) - timedelta(days=settings.retention_webhooks_days + 1)
    recent = datetime.now(UTC) - timedelta(days=1)
    with SessionLocal() as s:
        for event_id, status, received in [
            ("old_failed", WebhookEventStatus.FAILED, old),
            ("old_applied", WebhookEventStatus.APPLIED, old),
            ("old_dead", WebhookEventStatus.DEAD_LETTER, old),
            ("recent_dead", WebhookEventStatus.DEAD_LETTER, recent),
            ("recent_applied", WebhookEventStatus.APPLIED, recent),
        ]:
            s.add(
                WebhookEvent(
                    provider_event_id=event_id, kind="ignored", status=status, received_at=received
                )
            )
        s.commit()
    with SessionLocal() as s:
        api._sweep_retention(s)  # pyright: ignore[reportPrivateUsage]
        s.commit()
    with SessionLocal() as s:
        kept = {r.provider_event_id for r in s.scalars(select(WebhookEvent)).all()}
    assert kept == {"old_failed", "recent_dead", "recent_applied"}


# ---------- operator handles ----------


def _seed_rows() -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    with SessionLocal() as s:
        for n, status in enumerate(
            [WebhookEventStatus.APPLIED, WebhookEventStatus.FAILED, WebhookEventStatus.DEAD_LETTER]
        ):
            event_id = f"evt_{status.value}"
            r = WebhookEvent(
                id=uuid4(),
                provider_event_id=event_id,
                kind="account_updated",
                object_id="acct_fake_s1",
                status=status,
                attempts=0 if status is WebhookEventStatus.APPLIED else 3,
                last_error=None if status is WebhookEventStatus.APPLIED else "RuntimeError: x",
                received_at=datetime.now(UTC) - timedelta(minutes=10 - n),
                next_attempt_at=datetime.now(UTC) + timedelta(hours=1)
                if status is WebhookEventStatus.FAILED
                else None,
                payload={
                    "event_id": event_id,
                    "kind": "account_updated",
                    "object_id": "acct_fake_s1",
                    "payments_ready": False,
                    "amount_minor": None,
                    "outcome": None,
                    "related_id": None,
                }
                | ({"smuggled": "never exposed"} if status is WebhookEventStatus.APPLIED else {}),
            )
            s.add(r)
            ids[status.value] = r.id
        s.commit()
    return ids


def test_admin_lists_webhooks_with_status_filter_and_pagination(
    client: TestClient, admin: Header
) -> None:
    _seed_rows()
    all_rows = client.get("/v1/admin/webhooks", headers=admin).json()
    assert [r["provider_event_id"] for r in all_rows] == [
        "evt_dead_letter",
        "evt_failed",
        "evt_applied",
    ]  # newest first
    dead = client.get("/v1/admin/webhooks", params={"status": "dead_letter"}, headers=admin)
    [only] = dead.json()
    assert only["status"] == "dead_letter"
    assert only["attempts"] == 3
    assert only["last_error"] == "RuntimeError: x"
    assert only["payload"] == {
        "event_id": "evt_dead_letter",
        "kind": "account_updated",
        "object_id": "acct_fake_s1",
        "payments_ready": False,
        "amount_minor": None,
        "outcome": None,
        "related_id": None,
    }
    applied = client.get("/v1/admin/webhooks", params={"status": "applied"}, headers=admin)
    assert "smuggled" not in applied.json()[0]["payload"]  # declared fields only
    page = client.get("/v1/admin/webhooks", params={"limit": 1, "offset": 1}, headers=admin)
    assert [r["provider_event_id"] for r in page.json()] == ["evt_failed"]
    assert client.get("/v1/admin/webhooks", params={"limit": 0}, headers=admin).status_code == 422


def test_admin_webhook_endpoints_require_admin(client: TestClient, auth: AuthFactory) -> None:
    ids = _seed_rows()
    buyer = auth("buyer", "alice")
    assert client.get("/v1/admin/webhooks").status_code == 401
    assert client.get("/v1/admin/webhooks", headers=buyer).status_code == 403
    r = client.post(f"/v1/admin/webhooks/{ids['dead_letter']}/retry", headers=buyer)
    assert r.status_code == 403


def test_admin_requeues_dead_letter_and_the_job_applies_it(
    client: TestClient, auth: AuthFactory, admin: Header
) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    ids = _seed_rows()

    r = client.post(f"/v1/admin/webhooks/{ids['dead_letter']}/retry", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["attempts"] == 0
    assert datetime.fromisoformat(body["next_attempt_at"]) <= datetime.now(UTC)
    with SessionLocal() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "retry_webhook")) is not None

    api._run_webhook_retry_once()  # pyright: ignore[reportPrivateUsage]  # due now: no time travel

    assert row("evt_dead_letter").status is WebhookEventStatus.APPLIED
    with SessionLocal() as s:
        profile = s.get(SellerProfile, "s1")
        assert profile is not None and profile.payments_ready is False


def test_admin_requeues_failed_row_now(client: TestClient, admin: Header) -> None:
    ids = _seed_rows()
    r = client.post(f"/v1/admin/webhooks/{ids['failed']}/retry", headers=admin)
    assert r.status_code == 200
    assert r.json()["attempts"] == 0
    assert datetime.fromisoformat(r.json()["next_attempt_at"]) <= datetime.now(UTC)


def test_admin_retry_refuses_applied_unknown_and_payloadless(
    client: TestClient, admin: Header
) -> None:
    ids = _seed_rows()
    r = client.post(f"/v1/admin/webhooks/{ids['applied']}/retry", headers=admin)
    assert r.status_code == 409
    assert client.post(f"/v1/admin/webhooks/{uuid4()}/retry", headers=admin).status_code == 404
    with SessionLocal() as s:
        bare = WebhookEvent(
            id=uuid4(),
            provider_event_id="evt_bare",
            kind="ignored",
            status=WebhookEventStatus.DEAD_LETTER,
        )
        s.add(bare)
        s.commit()
    assert client.post(f"/v1/admin/webhooks/{bare.id}/retry", headers=admin).status_code == 409
