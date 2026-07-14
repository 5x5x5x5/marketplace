"""Transactional-outbox notifications: enqueue, renderers, drain, emitters."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Notification, User
from marketplace.mail import RecordingEmailSender
from marketplace.models import EventKind, NotificationStatus, PaymentStatus, UserRole
from marketplace.notifications import RENDERERS, drain_once, enqueue, enqueue_admins
from marketplace.payments.fake import FakeProvider
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail, pending_accept


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


# ---------- Emitters (through the API) ----------


def _drain() -> RecordingEmailSender:
    recorder = RecordingEmailSender()
    drain_once(recorder)
    return recorder


def _mail_to(recorder: RecordingEmailSender, addr_part: str) -> list[tuple[str, str, str]]:
    return [m for m in recorder.sent if addr_part in m[0]]


def test_offer_email_reaches_seller_with_payout_not_price(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    recorder = _drain()
    seller_mail = _mail_to(recorder, "s1@")
    assert len(seller_mail) == 1
    _, subject, body = seller_mail[0]
    assert basic_service in subject
    assert "14.00" in body  # seller payout
    assert "20.00" not in body  # buyer price must NEVER reach the seller
    assert "expires" in body.lower()


def test_accepted_email_with_payment_due_line(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    recorder = _drain()
    buyer_mail = _mail_to(recorder, "alice@")
    assert len(buyer_mail) == 1
    body = buyer_mail[0][2]
    assert "Complete your payment" in body
    assert "20.00" in body
    assert "14.00" not in body  # seller payout must NEVER reach the buyer


def test_accepted_email_without_payment_due_when_instant(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    recorder = _drain()
    body = _mail_to(recorder, "alice@")[0][2]
    assert "Complete your payment" not in body


def test_completed_email(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    _drain()  # clear offer+accepted mail
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    recorder = _drain()
    assert "complete" in _mail_to(recorder, "alice@")[0][1].lower()


def test_expired_no_seller_email(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job = new_job(client, auth, basic_service, "alice")  # nobody onboarded -> expires
    assert job["status"] == "expired"
    recorder = _drain()
    body = _mail_to(recorder, "alice@")[0][2]
    assert "no seller available" in body


def test_expired_payment_timeout_email(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, _pid = pending_accept(client, auth, basic_service, fake_payments)
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        assert job is not None
        job.accepted_at = datetime.now(UTC) - timedelta(minutes=999)
        s.commit()
    # A live principal's read triggers the sweep.
    client.get("/v1/seller/offers", headers=auth("seller", "s1"))
    recorder = _drain()
    bodies = [m[2] for m in _mail_to(recorder, "alice@")]
    assert any("payment window elapsed" in b for b in bodies)


def test_cancel_after_accept_informs_seller_and_refund_informs_buyer(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # instant success -> paid
    _drain()  # clear offer+accepted mail
    r = client.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin)
    assert r.status_code == 200
    recorder = _drain()
    seller_body = _mail_to(recorder, "s1@")[0][2]
    assert "cancelled" in seller_body
    assert "14.00" in seller_body and "20.00" not in seller_body
    buyer_bodies = [m[2] for m in _mail_to(recorder, "alice@")]
    assert any("refunded in full" in b for b in buyer_bodies)


def test_buyer_self_cancel_no_buyer_email_but_seller_informed(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, _pid = pending_accept(client, auth, basic_service, fake_payments)
    _drain()  # clear offer+accepted mail
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    recorder = _drain()
    assert _mail_to(recorder, "s1@")  # seller had accepted -> informed
    assert not _mail_to(recorder, "alice@")  # voided charge, no refund, no self-mail


def test_payout_failure_reaches_admin(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    # The admin fixture creates the ops admin user; payout failure fans out to it.
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    _drain()
    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    recorder = _drain()
    admin_mail = _mail_to(recorder, "ops@")
    assert len(admin_mail) == 1
    assert "FAILED" in admin_mail[0][1]
    assert "retry" in admin_mail[0][2].lower()


# ---------- SMTP adapter ----------


def test_smtp_sender_speaks_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    from marketplace.mail import SmtpEmailSender

    calls: list[tuple[str, object]] = []

    class _FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 0) -> None:
            calls.append(("connect", (host, port)))

        def __enter__(self) -> "_FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            calls.append(("quit", ()))

        def starttls(self) -> None:
            calls.append(("starttls", ()))

        def login(self, user: str, password: str) -> None:
            calls.append(("login", (user, password)))

        def send_message(self, message: object) -> None:
            calls.append(("send", message))

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    sender = SmtpEmailSender(
        host="mail.example.test",
        port=587,
        username="u",
        password="p",
        starttls=True,
        from_addr="noreply@example.test",
    )
    sender.send("to@example.test", "Subject!", "Body text")

    kinds = [c[0] for c in calls]
    assert kinds == ["connect", "starttls", "login", "send", "quit"]
    message = calls[3][1]
    assert message["From"] == "noreply@example.test"  # pyright: ignore[reportIndexIssue]
    assert message["To"] == "to@example.test"  # pyright: ignore[reportIndexIssue]
    assert message["Subject"] == "Subject!"  # pyright: ignore[reportIndexIssue]


def test_smtp_sender_skips_login_and_tls_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smtplib

    from marketplace.mail import SmtpEmailSender

    kinds: list[str] = []

    class _FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 0) -> None:
            kinds.append("connect")

        def __enter__(self) -> "_FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            kinds.append("quit")

        def send_message(self, message: object) -> None:
            kinds.append("send")

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    SmtpEmailSender(
        host="mailpit.local",
        port=1025,
        username="",
        password="",
        starttls=False,
        from_addr="noreply@example.test",
    ).send("to@example.test", "s", "b")
    assert kinds == ["connect", "send", "quit"]


# ---------- Admin endpoints + maintenance loop pieces ----------


def test_admin_notifications_list_and_filter(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    pending = client.get(
        "/v1/admin/notifications", params={"status": "pending"}, headers=admin
    ).json()
    assert len(pending) == 1
    assert pending[0]["kind"] == "offer_received"
    assert pending[0]["attempts"] == 0
    _drain()
    assert (
        client.get("/v1/admin/notifications", params={"status": "pending"}, headers=admin).json()
        == []
    )
    sent = client.get("/v1/admin/notifications", params={"status": "sent"}, headers=admin).json()
    assert len(sent) == 1 and sent[0]["sent_at"] is not None


def test_admin_manual_drain(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    r = client.post("/v1/admin/notifications/drain", headers=admin)
    assert r.status_code == 200
    assert r.json() == {"sent": 1}  # console sender: send is a log line, still counts
    audit = client.get("/v1/admin/audit", headers=admin).json()
    assert any(a["action"] == "drain_notifications" for a in audit)


@pytest.mark.skipif(not IS_POSTGRES, reason="SKIP LOCKED is only real on Postgres")
def test_concurrent_drain_never_double_sends(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    onboard_and_avail(client, auth, basic_service, "s1")
    for buyer in ("a1", "a2", "a3", "a4", "a5"):
        new_job(client, auth, basic_service, buyer)
    recorder = RecordingEmailSender()  # list.append is GIL-atomic

    def _one_drain(_index: int) -> int:
        return drain_once(recorder)

    with ThreadPoolExecutor(max_workers=2) as pool:
        totals = list(pool.map(_one_drain, range(2)))
    # Every offer email exists exactly once across both drains - no row sent twice.
    offer_mails = [m for m in recorder.sent if "New offer" in m[1]]
    assert sum(totals) == len(recorder.sent)
    job_ids = [m[2].rsplit("Job: ", 1)[1] for m in offer_mails]
    assert len(job_ids) == len(set(job_ids))
