# Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transactional-outbox email notifications for the seven core lifecycle events, drained by an in-process asyncio loop that also runs the sweeps — closing the background-scheduler roadmap item with zero new dependencies.

**Architecture:** Domain transitions `enqueue` a `notifications` row in the same transaction; `notifications.drain_once` claims due rows with `FOR UPDATE SKIP LOCKED` and sends via the mail port with retry/backoff; `api.py`'s lifespan spawns `_maintenance_loop` (drain every 5s, `_sweep` every 60s via `asyncio.to_thread`). A stdlib-`smtplib` adapter makes delivery real when `SMTP_HOST` is set. Import direction stays one-way: `api → notifications → (mail, db, entities, models)`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + Alembic, asyncio + stdlib smtplib/email.message — **zero new dependencies**.

**Spec:** `docs/superpowers/specs/2026-07-14-notifications-design.md` (approved). Branch: `notifications`.

## Global Constraints

- Package manager is `uv`; gate after every task: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` — all green, pyright **strict**.
- **Never gate on a piped test command** — run bare, check exit codes.
- **Zero new dependencies** — asyncio, smtplib, email.message are stdlib.
- The PostToolUse formatter deletes not-yet-used imports; add imports with their usage.
- No backticks in double-quoted `git commit -m`. Stage exact paths, never `git add -A`. End commit messages with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- ORM never leaves the API layer; identity from principal only; pricing/matching core untouched.
- **Information asymmetry in payloads**: seller payloads/emails never contain `buyer_price`; buyer payloads/emails never contain `seller_payout`. Tests must assert both directions.
- Tests are deterministic: the `client` fixture never runs the lifespan, so the loop never runs in tests — tests call `drain_once()` directly.
- **Before the branch is declared done, the suite must be green on BOTH SQLite and Postgres** (`DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace`, container `marketplace-db-1`; run `alembic upgrade head` there first). This is Task 6's gate, learned from the auth build's FK incident.
- `tests/test_notifications.py` may import the public helpers from `tests/test_payments.py` (`onboard_and_avail`, `new_job`, `_accept_first_offer`, `_pending_accept`) — established convention.

---

### Task 1: Enums, Notification entity, migration #4

**Files:**
- Modify: `src/marketplace/models.py` (two enums after `PayoutStatus` at line 68; `NotificationOut` in the views section)
- Modify: `src/marketplace/entities.py` (entity after `EmailToken`; extend the `.models` import at line 32)
- Create: `migrations/versions/<autogen>_notifications.py`
- Test: `tests/test_notifications.py` (new)

**Interfaces:**
- Produces: `models.EventKind` (`OFFER_RECEIVED="offer_received"`, `JOB_ACCEPTED_BUYER="job_accepted_buyer"`, `JOB_COMPLETED_BUYER="job_completed_buyer"`, `JOB_EXPIRED_BUYER="job_expired_buyer"`, `JOB_CANCELLED_SELLER="job_cancelled_seller"`, `REFUND_ISSUED_BUYER="refund_issued_buyer"`, `PAYOUT_FAILED_ADMIN="payout_failed_admin"`); `models.NotificationStatus` (`PENDING/SENT/FAILED`); `models.NotificationOut`; `entities.Notification` (fields below).

- [ ] **Step 1: Write the failing test**

Create `tests/test_notifications.py`:

```python
"""Transactional-outbox notifications: enqueue, renderers, drain, emitters."""


def test_notifications_table_registered() -> None:
    from marketplace.entities import Base

    assert "notifications" in Base.metadata.tables
```

Run: `uv run pytest tests/test_notifications.py -q`
Expected: FAIL — assertion error.

- [ ] **Step 2: Add enums + view to `src/marketplace/models.py`**

After `PayoutStatus`:

```python
class EventKind(StrEnum):
    OFFER_RECEIVED = "offer_received"  # seller: the 2-minute clock is ticking
    JOB_ACCEPTED_BUYER = "job_accepted_buyer"
    JOB_COMPLETED_BUYER = "job_completed_buyer"
    JOB_EXPIRED_BUYER = "job_expired_buyer"
    JOB_CANCELLED_SELLER = "job_cancelled_seller"
    REFUND_ISSUED_BUYER = "refund_issued_buyer"
    PAYOUT_FAILED_ADMIN = "payout_failed_admin"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # terminal after notify_max_attempts; inspect via admin endpoint
```

In the response-views section (near the other `*Out` models):

```python
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: str
    email: str
    kind: EventKind
    status: NotificationStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
```

- [ ] **Step 3: Add the entity to `src/marketplace/entities.py`**

Extend the models import (line 32) with `EventKind, NotificationStatus`. After `EmailToken`:

```python
class Notification(Base):
    """Transactional-outbox row: enqueued inside the domain transaction,
    delivered by the drainer. `email` and `payload` are snapshots taken at
    enqueue time — the drainer never re-queries mutable domain state."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    kind: Mapped[EventKind] = mapped_column(_enum(EventKind))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus), default=NotificationStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(_TS, default=_now, index=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
    last_error: Mapped[str | None] = mapped_column(String(512), default=None)
```

- [ ] **Step 4: Run the schema test**

Run: `uv run pytest tests/test_notifications.py -q` → 1 passed.

- [ ] **Step 5: Generate + verify migration #4**

```bash
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic revision --autogenerate -m "notifications"
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
```

Verify: one new table; `UTCDateTime` renders as `sa.DateTime(timezone=True)`; no `marketplace.` import; indexes on `user_id`, `status`, `next_attempt_at`; final from-scratch upgrade applies all four migrations, exit 0.

- [ ] **Step 6: Full gate + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → green (120 collected + the new one).

```bash
git add src/marketplace/models.py src/marketplace/entities.py migrations/versions/ tests/test_notifications.py
git commit -m "Add Notification outbox entity, event enums, and migration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: notifications.py core — enqueue, renderers, drain_once

**Files:**
- Create: `src/marketplace/notifications.py`
- Modify: `src/marketplace/settings.py` (drain settings)
- Test: `tests/test_notifications.py`

**Interfaces:**
- Consumes: Task 1's entity/enums; `mail.EmailSender`/`RecordingEmailSender`; `db.SessionLocal`; `entities.User`; `models.UserRole`.
- Produces (Tasks 3/5/6 rely on): `notifications.enqueue(session: Session, kind: EventKind, user_id: str, payload: dict[str, Any]) -> None`; `notifications.enqueue_admins(session: Session, kind: EventKind, payload: dict[str, Any]) -> None`; `notifications.drain_once(mail: EmailSender, limit: int = 20) -> int`; `notifications.RENDERERS: dict[EventKind, Callable[[dict[str, Any]], tuple[str, str]]]`; settings `notify_drain_seconds: int = 5`, `notify_max_attempts: int = 5`, `sweep_interval_seconds: int = 60`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifications.py` (imports at top, with usage):

```python
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Notification, User
from marketplace.mail import RecordingEmailSender
from marketplace.models import EventKind, NotificationStatus, UserRole
from marketplace.notifications import RENDERERS, drain_once, enqueue, enqueue_admins


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
```

Run: `uv run pytest tests/test_notifications.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marketplace.notifications'`.

- [ ] **Step 2: Add drain settings to `src/marketplace/settings.py`**

After the auth block:

```python
    # Notifications: transactional outbox drained by the in-process loop.
    notify_drain_seconds: int = 5
    notify_max_attempts: int = 5
    sweep_interval_seconds: int = 60
```

- [ ] **Step 3: Create `src/marketplace/notifications.py`**

```python
"""Transactional-outbox notifications.

Domain transitions call `enqueue` INSIDE their transaction, so rolled-back
events never mail and committed ones eventually always do. `drain_once` —
called by the maintenance loop in api.py and the admin drain endpoint —
claims due rows and sends them through the mail port with retry/backoff.

Payloads are role-safe snapshots built at enqueue time: seller payloads never
contain buyer_price, buyer payloads never contain seller_payout. Import
direction is one-way: api -> notifications -> (mail, db, entities, models).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .entities import Notification, User
from .mail import EmailSender
from .models import EventKind, NotificationStatus, UserRole
from .settings import settings

logger = logging.getLogger("marketplace.notifications")

_BACKOFF_BASE_SECONDS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(session: Session, kind: EventKind, user_id: str, payload: dict[str, Any]) -> None:
    """Queue a notification in the caller's transaction. A missing recipient is
    a logged skip — a stale id must never break a money transaction."""
    user = session.get(User, user_id)
    if user is None:
        logger.warning("notification %s skipped: no user %s", kind, user_id)
        return
    session.add(Notification(user_id=user.id, email=user.email, kind=kind, payload=payload))


def enqueue_admins(session: Session, kind: EventKind, payload: dict[str, Any]) -> None:
    """Fan a notification out to every admin account; none -> logged skip."""
    admins = session.scalars(select(User).where(User.role == UserRole.ADMIN)).all()
    if not admins:
        logger.warning("notification %s skipped: no admin accounts", kind)
        return
    for admin in admins:
        session.add(Notification(user_id=admin.id, email=admin.email, kind=kind, payload=payload))


# ---------- Renderers: pure payload -> (subject, body) ----------


def _render_offer_received(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"New offer: {p['service_type_id']} paying {p['seller_payout']}",
        (
            f"You have a new offer for {p['service_type_id']} paying {p['seller_payout']}.\n"
            f"It expires at {p['expires_at']} - accept it in the app before then.\n"
            f"Job: {p['job_id']}"
        ),
    )


def _render_job_accepted_buyer(p: dict[str, Any]) -> tuple[str, str]:
    lines = [
        f"A seller accepted your {p['service_type_id']} job ({p['buyer_price']}).",
        f"Job: {p['job_id']}",
    ]
    if p.get("awaiting_payment"):
        lines.insert(1, "Complete your payment in the app to start the work.")
    return (f"Your {p['service_type_id']} job was accepted", "\n".join(lines))


def _render_job_completed_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Your {p['service_type_id']} job is complete",
        (
            f"Your {p['service_type_id']} job ({p['buyer_price']}) is complete.\n"
            f"You can leave a review in the app.\nJob: {p['job_id']}"
        ),
    )


def _render_job_expired_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Your {p['service_type_id']} job expired",
        (
            f"Your {p['service_type_id']} job expired: {p['reason']}.\n"
            f"You can request a new quote any time.\nJob: {p['job_id']}"
        ),
    )


def _render_job_cancelled_seller(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Job cancelled: {p['service_type_id']}",
        (
            f"The {p['service_type_id']} job you accepted ({p['seller_payout']}) was cancelled.\n"
            f"Your slot is free again.\nJob: {p['job_id']}"
        ),
    )


def _render_refund_issued_buyer(p: dict[str, Any]) -> tuple[str, str]:
    return (
        "Your payment was refunded",
        f"Your payment of {p['buyer_price']} was refunded in full.\nJob: {p['job_id']}",
    )


def _render_payout_failed_admin(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Payout FAILED for job {p['job_id']}",
        (
            f"Transfer of {p['amount']} to seller {p['seller_id']} failed.\n"
            f"Retry via POST /v1/admin/payouts/{p['payout_id']}/retry.\nJob: {p['job_id']}"
        ),
    )


RENDERERS: dict[EventKind, Callable[[dict[str, Any]], tuple[str, str]]] = {
    EventKind.OFFER_RECEIVED: _render_offer_received,
    EventKind.JOB_ACCEPTED_BUYER: _render_job_accepted_buyer,
    EventKind.JOB_COMPLETED_BUYER: _render_job_completed_buyer,
    EventKind.JOB_EXPIRED_BUYER: _render_job_expired_buyer,
    EventKind.JOB_CANCELLED_SELLER: _render_job_cancelled_seller,
    EventKind.REFUND_ISSUED_BUYER: _render_refund_issued_buyer,
    EventKind.PAYOUT_FAILED_ADMIN: _render_payout_failed_admin,
}


def drain_once(mail: EmailSender, limit: int = 20) -> int:
    """Send due pending rows; returns the number sent.

    Safe to run concurrently: rows are claimed FOR UPDATE SKIP LOCKED (real on
    Postgres, serialized single-writer on SQLite). Per-row try/except - one bad
    row backs off without blocking the queue."""
    sent = 0
    with SessionLocal() as session:
        rows = session.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.PENDING,
                Notification.next_attempt_at <= _now(),
            )
            .order_by(Notification.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                subject, body = RENDERERS[row.kind](row.payload)
                mail.send(row.email, subject, body)
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)[:512]
                if row.attempts >= settings.notify_max_attempts:
                    row.status = NotificationStatus.FAILED
                else:
                    row.next_attempt_at = _now() + timedelta(
                        seconds=_BACKOFF_BASE_SECONDS * 2**row.attempts
                    )
                continue
            row.status = NotificationStatus.SENT
            row.sent_at = _now()
            sent += 1
        session.commit()
    return sent
```

- [ ] **Step 4: Run tests + gate**

Run: `uv run pytest tests/test_notifications.py -q` → all pass.
Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add src/marketplace/notifications.py src/marketplace/settings.py tests/test_notifications.py
git commit -m "Add outbox core: enqueue, renderers, drain_once with backoff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: The seven emitters

**Files:**
- Modify: `src/marketplace/api.py` — `_create_offer` (:106), `_match_and_offer` (:118), `_sweep_stale_payments` (:153), `cancel_job` (:324), `_release_payment` (:386, now returns bool), `accept_offer` (:514), `complete_job` (:597), `admin_cancel_job` (:875), `_apply_payment_event` (:911)
- Test: `tests/test_notifications.py`

**Interfaces:**
- Consumes: `notifications.enqueue`/`enqueue_admins`/`drain_once`, `EventKind` (Task 2); existing test helpers from `tests/test_payments.py`.
- Produces: `_release_payment(session, provider, job) -> bool` (True when a refund was issued) — both cancel endpoints consume the changed signature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifications.py` (extend imports with usage: `from fastapi.testclient import TestClient`, `from tests.conftest import AuthFactory, Header`, `from tests.test_payments import _accept_first_offer, _pending_accept, new_job, onboard_and_avail`, `from marketplace.models import PaymentStatus`, `from marketplace.payments.fake import FakeProvider`, `from uuid import UUID` as needed):

```python
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
    _accept_first_offer(client, auth("seller", "s1"))
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
    _accept_first_offer(client, auth("seller", "s1"))
    recorder = _drain()
    body = _mail_to(recorder, "alice@")[0][2]
    assert "Complete your payment" not in body


def test_completed_email(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    _drain()  # clear offer+accepted mail
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    recorder = _drain()
    assert "complete" in _mail_to(recorder, "alice@")[0][1].lower()


def test_expired_no_seller_email(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job = new_job(client, auth, basic_service, "alice")  # nobody onboarded -> expires
    assert job["status"] == "expired"
    recorder = _drain()
    body = _mail_to(recorder, "alice@")[0][2]
    assert "no seller available" in body


def test_expired_payment_timeout_email(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, _pid = _pending_accept(client, auth, basic_service, fake_payments)
    from marketplace.entities import Job

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
    _accept_first_offer(client, auth("seller", "s1"))  # instant success -> paid
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
    job_id, _pid = _pending_accept(client, auth, basic_service, fake_payments)
    _drain()  # clear offer+accepted mail
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    recorder = _drain()
    assert _mail_to(recorder, "s1@")  # seller had accepted -> informed
    assert not _mail_to(recorder, "alice@")  # voided charge, no refund, no self-mail


def test_payout_failure_reaches_admin(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header,
    fake_payments: FakeProvider,
) -> None:
    # The admin fixture creates the ops admin user; payout failure fans out to it.
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    _drain()
    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    recorder = _drain()
    admin_mail = _mail_to(recorder, "ops@")
    assert len(admin_mail) == 1
    assert "FAILED" in admin_mail[0][1]
    assert "retry" in admin_mail[0][2].lower()
```

Run: `uv run pytest tests/test_notifications.py -q`
Expected: the new tests FAIL (drain finds nothing — no emitters yet).

- [ ] **Step 2: Wire the emitters in `src/marketplace/api.py`**

Imports (with usage, same edit): `from . import notifications`; extend the models import with `EventKind`.

`_create_offer` (:106) — hoist the expiry and enqueue after the add:

```python
def _create_offer(session: Session, job: Job, seller_id: str, payout: Any) -> None:
    expires_at = _now() + timedelta(minutes=settings.offer_ttl_minutes)
    session.add(
        Offer(
            job_id=job.id,
            service_type_id=job.service_type_id,
            seller_id=seller_id,
            seller_payout=payout,
            expires_at=expires_at,
        )
    )
    notifications.enqueue(
        session,
        EventKind.OFFER_RECEIVED,
        seller_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "seller_payout": str(payout),
            "expires_at": expires_at.isoformat(),
        },
    )
```

`_match_and_offer` (:118) — both EXPIRED branches route through one helper placed just above it:

```python
def _expire_unmatched(session: Session, job: Job) -> None:
    job.status = JobStatus.EXPIRED
    notifications.enqueue(
        session,
        EventKind.JOB_EXPIRED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "reason": "no seller available",
        },
    )
```

(Replace the two `job.status = JobStatus.EXPIRED` lines inside `_match_and_offer` with `_expire_unmatched(session, job)`.)

`_sweep_stale_payments` (:153) — after `locked_job.status = JobStatus.EXPIRED`:

```python
        notifications.enqueue(
            session,
            EventKind.JOB_EXPIRED_BUYER,
            locked_job.buyer_id,
            {
                "job_id": str(locked_job.id),
                "service_type_id": locked_job.service_type_id,
                "reason": "payment window elapsed",
            },
        )
```

`_release_payment` (:386) — now reports whether it refunded:

```python
def _release_payment(session: Session, provider: PaymentProvider, job: Job) -> bool:
    """Undo whatever the job's charge collected. Returns True when a refund was
    issued (vs voided/no-op) so callers can notify the buyer."""
```

…body unchanged except `return True` after the refund branch sets `REFUNDED`, `return False` at the void branch's end and the no-payment early return.

`accept_offer` (:514) — after the `job.status = (...)` assignment, before `session.flush()`:

```python
    notifications.enqueue(
        session,
        EventKind.JOB_ACCEPTED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "buyer_price": str(job.buyer_price),
            "awaiting_payment": job.status is JobStatus.AWAITING_PAYMENT,
        },
    )
```

`complete_job` (:597) — after `session.add(payout)`, add a flush so `payout.id` exists, then:

```python
    session.flush()
    notifications.enqueue(
        session,
        EventKind.JOB_COMPLETED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "buyer_price": str(job.buyer_price),
        },
    )
    if payout.status is PayoutStatus.FAILED:
        notifications.enqueue_admins(
            session,
            EventKind.PAYOUT_FAILED_ADMIN,
            {
                "job_id": str(job.id),
                "payout_id": str(payout.id),
                "seller_id": seller_id,
                "amount": str(job.seller_payout),
            },
        )
```

`cancel_job` (:324) and `admin_cancel_job` (:875) — capture the release result and enqueue before returning (identical block in both, matching their existing symmetry):

```python
    try:
        refunded = _release_payment(session, provider, job)
    except PaymentError:
        ...existing 502 handling unchanged...
    if job.seller_id is not None:
        notifications.enqueue(
            session,
            EventKind.JOB_CANCELLED_SELLER,
            job.seller_id,
            {
                "job_id": str(job.id),
                "service_type_id": job.service_type_id,
                "seller_payout": str(job.seller_payout),
            },
        )
    if refunded:
        notifications.enqueue(
            session,
            EventKind.REFUND_ISSUED_BUYER,
            job.buyer_id,
            {"job_id": str(job.id), "buyer_price": str(job.buyer_price)},
        )
    job.status = JobStatus.CANCELLED
```

`_apply_payment_event` (:911), transfer_failed branch — where the payout flips to FAILED:

```python
            if event.kind == "transfer_failed":
                notifications.enqueue_admins(
                    session,
                    EventKind.PAYOUT_FAILED_ADMIN,
                    {
                        "job_id": str(payout.job_id),
                        "payout_id": str(payout.id),
                        "seller_id": payout.seller_id,
                        "amount": str(payout.amount),
                    },
                )
```

(Restructure that branch minimally so the enqueue happens only on the FAILED assignment, not on transfer_paid.)

- [ ] **Step 3: Run tests + full suite**

Run: `uv run pytest tests/test_notifications.py -q` → all pass.
Run: `uv run pytest -q` → everything green (existing suites unaffected: enqueue rows are invisible to them, `clean_tables` wipes the outbox).

- [ ] **Step 4: Gate + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright` → clean.

```bash
git add src/marketplace/api.py tests/test_notifications.py
git commit -m "Emit outbox notifications at the seven lifecycle transitions

Role-safe payload snapshots at enqueue; _release_payment reports
refunds so cancels can notify the buyer.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: SMTP adapter + mail settings

**Files:**
- Modify: `src/marketplace/mail.py`
- Modify: `src/marketplace/settings.py`
- Modify: `tests/conftest.py` (one env pin)
- Test: `tests/test_notifications.py`

**Interfaces:**
- Produces: `mail.SmtpEmailSender(host: str, port: int, username: str, password: str, starttls: bool, from_addr: str)` implementing `EmailSender`; `get_mail_sender()` now returns SMTP when `settings.smtp_host` is set (console otherwise); settings `smtp_host=""`, `smtp_port=587`, `smtp_username=""`, `smtp_password=""`, `smtp_starttls=True`, `mail_from="marketplace@localhost"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifications.py`:

```python
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
        host="mailpit.local", port=1025, username="", password="", starttls=False,
        from_addr="noreply@example.test",
    ).send("to@example.test", "s", "b")
    assert kinds == ["connect", "send", "quit"]
```

(`import pytest` is already present in the file's imports if earlier tests used it; add with usage otherwise.)

Run: `uv run pytest tests/test_notifications.py -q`
Expected: the 2 new tests FAIL — `ImportError: cannot import name 'SmtpEmailSender'`.

- [ ] **Step 2: Add SMTP settings to `src/marketplace/settings.py`**

After the notifications block:

```python
    # Mail delivery: SMTP_HOST set -> stdlib SMTP adapter (any provider's SMTP
    # endpoint, or Mailpit locally); empty -> console adapter (logs only).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = "marketplace@localhost"
```

- [ ] **Step 3: Pin the test env in `tests/conftest.py`**

Next to the Stripe pins (same rationale — a developer `.env` with SMTP config must never make the suite send real mail):

```python
os.environ.setdefault("SMTP_HOST", "")
```

- [ ] **Step 4: Add the adapter to `src/marketplace/mail.py`**

Imports with usage: `import smtplib`, `from email.message import EmailMessage`, `from .settings import settings`. After `RecordingEmailSender`:

```python
class SmtpEmailSender:
    """Real delivery via any provider's SMTP endpoint. Stdlib only; STARTTLS
    then LOGIN when configured, plain relay (e.g. Mailpit) when not."""

    def __init__(
        self, host: str, port: int, username: str, password: str, starttls: bool, from_addr: str
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._starttls = starttls
        self._from_addr = from_addr

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from_addr
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)
```

Replace the `_active` initialization:

```python
def _default_sender() -> EmailSender:
    if settings.smtp_host:
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            starttls=settings.smtp_starttls,
            from_addr=settings.mail_from,
        )
    return ConsoleEmailSender()


_active: EmailSender = _default_sender()
```

- [ ] **Step 5: Run tests + gate + commit**

Run: `uv run pytest tests/test_notifications.py -q` → pass. Full gate → green (the Task 1 mail-swap test's final `isinstance(get_mail_sender(), ConsoleEmailSender)` assertion still holds because conftest pins `SMTP_HOST=""`).

```bash
git add src/marketplace/mail.py src/marketplace/settings.py tests/conftest.py tests/test_notifications.py
git commit -m "Add stdlib SMTP adapter selected by SMTP_HOST

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Maintenance loop + admin endpoints

**Files:**
- Modify: `src/marketplace/api.py` (loop + lifespan at :963-area; two admin endpoints after `list_payouts`)
- Test: `tests/test_notifications.py`

**Interfaces:**
- Consumes: `notifications.drain_once`, `get_mail_sender`, `get_provider`, `_sweep`, `SessionLocal` (extend the `.db` import), `NotificationOut` (extend models import), `Notification` (extend entities import).
- Produces: `GET /v1/admin/notifications?status=` → `list[NotificationOut]`; `POST /v1/admin/notifications/drain` → `{"sent": int}` + audit row `drain_notifications`; `api._maintenance_loop()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifications.py`:

```python
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        totals = list(pool.map(lambda _: drain_once(recorder), range(2)))
    # Five offer emails exist across both drains - no row sent twice.
    offer_mails = [m for m in recorder.sent if "New offer" in m[1]]
    assert sum(totals) == len(offer_mails)
    job_ids = [m[2].rsplit("Job: ", 1)[1] for m in offer_mails]
    assert len(job_ids) == len(set(job_ids))
```

(Extend imports with usage: `import pytest`, `from tests.conftest import IS_POSTGRES`. Note some buyers' jobs may expire if capacity blocks matching — `s1` has capacity 1 but offers don't consume capacity, so all five jobs get offers; the assertion counts whatever offer mails exist and requires uniqueness plus totals agreement.)

Run: `uv run pytest tests/test_notifications.py -q`
Expected: the two admin tests FAIL (404); the PG test skips on SQLite.

- [ ] **Step 2: Add the loop to `src/marketplace/api.py`**

Imports with usage: `import asyncio`, `import time`, extend `from contextlib import asynccontextmanager` → `from contextlib import asynccontextmanager, suppress`, extend the `.db` import with `SessionLocal`.

Above `_lifespan`:

```python
def _run_maintenance_once() -> None:
    """One drain + sweep pass on a worker thread (sync Session stays off the loop)."""
    notifications.drain_once(get_mail_sender())


def _run_sweep_once() -> None:
    with SessionLocal() as session:
        _sweep(session, get_provider())
        session.commit()


async def _maintenance_loop() -> None:
    """The template's heartbeat: drain the outbox every few seconds and run the
    sweeps every minute, so offers/payments/sessions expire — and sellers get
    their 2-minute-TTL offer emails — even when no requests arrive. Ticks are
    crash-proof; cancellation (lifespan shutdown) stops the loop."""
    last_sweep = time.monotonic()
    while True:
        await asyncio.sleep(settings.notify_drain_seconds)
        try:
            await asyncio.to_thread(_run_maintenance_once)
        except Exception:
            logger.exception("notification drain tick failed")
        if time.monotonic() - last_sweep >= settings.sweep_interval_seconds:
            last_sweep = time.monotonic()
            try:
                await asyncio.to_thread(_run_sweep_once)
            except Exception:
                logger.exception("sweep tick failed")
```

`get_mail_sender` import: extend the existing mail-related imports (api.py does not import mail yet — add `from .mail import get_mail_sender` with this usage).

`_lifespan` becomes:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Create tables for SQLite/dev. Production applies Alembic migrations instead.
    if settings.database_url.startswith("sqlite"):
        init_db()
    bootstrap_admin()
    logger.info("marketplace starting (db=%s)", settings.database_url.split("://", 1)[0])
    maintenance = asyncio.create_task(_maintenance_loop())
    yield
    maintenance.cancel()
    with suppress(asyncio.CancelledError):
        await maintenance
```

- [ ] **Step 3: Add the admin endpoints**

Extend the models import with `NotificationOut, NotificationStatus`; the entities import with `Notification`. After `retry_payout` in the admin router:

```python
@admin_router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    session: SessionDep,
    status: NotificationStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Notification]:
    stmt = select(Notification)
    if status is not None:
        stmt = stmt.where(Notification.status == status)
    rows = session.scalars(stmt.order_by(Notification.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/notifications/drain")
def drain_notifications(session: SessionDep, admin_id: AdminId) -> dict[str, int]:
    """Manual drain for ops/cron — the in-process loop normally handles this."""
    sent = notifications.drain_once(get_mail_sender())
    audit(session, admin_id, "drain_notifications", "notifications", {"sent": sent})
    return {"sent": sent}
```

- [ ] **Step 4: Run tests + full suite + gate**

Run: `uv run pytest tests/test_notifications.py -q` → pass (PG test skipped on SQLite).
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → green. (No existing test enters the lifespan context, so the loop stays dormant in tests.)

- [ ] **Step 5: Commit**

```bash
git add src/marketplace/api.py tests/test_notifications.py
git commit -m "Add maintenance loop (drain + timed sweep) and admin notification endpoints

Closes the background-scheduler item: offers, payments, and sessions now
expire on a clock, and outbox mail flows without traffic.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Demo act, docs, both-backend verification

**Files:**
- Modify: `scripts/demo.py`, `.env.example`, `README.md`, `CLAUDE.md`, `ROADMAP.md`

**Interfaces:** none produced.

- [ ] **Step 1: Demo act — prove the LOOP delivers**

Read `scripts/demo.py` first; match its style. Before the app import, add `os.environ.setdefault("NOTIFY_DRAIN_SECONDS", "1")`. Append a notifications act after the existing acts, inside the `with TestClient(...)` block (the lifespan loop is live there):

```python
# --- Act: notifications (delivered by the maintenance loop, not an explicit drain) ---
from marketplace.mail import RecordingEmailSender, use_sender

outbox = RecordingEmailSender()
previous_sender = use_sender(outbox)
# Fresh quote -> job for the act-1 seller (adapt the header/service variable
# names to the script's; the seller must have a free capacity slot here — if
# act 1 leaves them busy, complete that job first or raise capacity via admin).
quote = c.post("/v1/quotes", json={"service_type_id": service_id}, headers=buyer_headers).json()
job = c.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer_headers).json()
assert job["status"] == "pending", job
deadline = time.time() + 15
while time.time() < deadline and not outbox.sent:
    time.sleep(0.5)
use_sender(previous_sender)
assert outbox.sent, "maintenance loop did not deliver the offer email within 15s"
offer_mail = outbox.sent[0]
print(f"step N: maintenance loop delivered: to={offer_mail[0]} subject={offer_mail[1]!r}")
assert "New offer" in offer_mail[1]
```

(`import time` at the top with usage. The bounded poll keeps the demo deterministic; the loop ticks every 1s under the env override. Adapt the job-creation lines to the script's existing helpers; capacity note: complete or use a second seller if act-1's seller is at capacity.)

Run: `uv run python scripts/demo.py` → exit 0, the new act prints a loop-delivered email.

- [ ] **Step 2: `.env.example`**

Append:

```bash
# Notifications - outbox drained in-process; SMTP unset -> console adapter (logs).
# NOTIFY_DRAIN_SECONDS=5
# NOTIFY_MAX_ATTEMPTS=5
# SWEEP_INTERVAL_SECONDS=60
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USERNAME=
# SMTP_PASSWORD=
# SMTP_STARTTLS=true
# MAIL_FROM=noreply@your-app.example
```

- [ ] **Step 3: Docs**

- README: notifications section — the seven events, outbox semantics (same-transaction enqueue), the maintenance loop (drain + sweep cadence, closes the scheduler gap), SMTP config (any provider's SMTP endpoint; Mailpit for local), admin observability endpoints.
- CLAUDE.md Non-negotiables: notifications are written ONLY via `notifications.enqueue`/`enqueue_admins` inside the domain transaction — never send mail from an endpoint; sends happen only in `drain_once`; payloads are role-safe snapshots at enqueue (seller payloads never carry buyer_price and vice versa). Subtle bits: the loop never runs in tests (client fixture skips lifespan; tests call `drain_once`); `SMTP_HOST` pinned empty in conftest.
- ROADMAP: **Notifications → Done** and **Background scheduler → Done** (folded into the maintenance loop; external-worker extraction needs no schema change); what's-ahead renumbers with trust & safety leading; note preferences/digests land there.

- [ ] **Step 4: Final verification — BOTH backends, exit codes visible**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
uv run python scripts/demo.py
rm -f /tmp/claude-1000/mig.db && DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest -q
```

Expected: SQLite suite green; demo exit 0; four migrations from scratch; **Postgres suite green including the new concurrent-drain test** (no skips besides none — the PG run executes both PG-gated tests). Check every exit code bare.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py .env.example README.md CLAUDE.md ROADMAP.md
git commit -m "Document notifications: loop-delivered demo act, env, README/CLAUDE/ROADMAP

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review checklist (run after writing, fixed inline)

- **Spec coverage:** outbox table + migration #4 (T1) · enqueue/enqueue_admins/renderers/drain_once with SKIP LOCKED + backoff + terminal FAILED (T2) · seven emitters with role-safe payloads, `_release_payment -> bool`, `_expire_unmatched` covering both EXPIRED branches (T3) · SMTP adapter + selection + conftest pin (T4) · maintenance loop in api.py (import direction preserved), lifespan wiring, admin list/drain endpoints (T5) · demo loop-proof act, docs, ROADMAP closes notifications AND scheduler, both-backend gate (T6). Deferred per spec: preferences, digests, payout receipts, review nudges.
- **Type consistency:** `drain_once(mail: EmailSender, limit: int = 20) -> int` used by tests (T2/T3/T5), admin endpoint (T5), demo (T6 via loop); `EventKind` members match renderer registry keys (T2 test asserts full coverage); `_release_payment` bool consumed by both cancels (T3); `RecordingEmailSender.sent` tuple order (to, subject, body) used consistently in `_mail_to`/asserts.
- **Known judgment calls:** payout-failure test relies on the `admin` fixture creating the ops admin user before completion — the fixture must be requested in the test signature even though the header itself is unused for the flow (it seeds the recipient). The PG concurrent-drain assertion counts unique job ids rather than a fixed 5 because matching outcomes depend on capacity timing.
- **Placeholders:** none — every step carries runnable code or exact commands.
