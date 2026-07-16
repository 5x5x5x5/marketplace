# Observability & Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the template runnable-by-one-person: request-id-correlated JSON logging, a clean 500 envelope, an admin stats endpoint, retention sweeps for the three unbounded tables, the webhook off the event loop, and the API-hardening ride-alongs.

**Architecture:** One new module `observability.py` owns the request boundary — logging config (stdlib dictConfig, JSON default / plain toggle), a `RequestIdMiddleware` that does request-id + access-log + error-envelope in one place (a separate `@app.exception_handler` would run OUTSIDE user middleware and lose the header/contextvar — this is load-bearing), and a `BodySizeLimitMiddleware`. Stats and retention ride the existing admin-router/`_sweep` machinery. Webhook DB work moves to `asyncio.to_thread` with its own session (the `_run_drain_once` precedent).

**Tech Stack:** FastAPI/Starlette pure-ASGI middleware, stdlib logging/contextvars, SQLAlchemy 2.0, Alembic (migration #10), pytest.

**Spec:** `docs/superpowers/specs/2026-07-15-observability-ops-design.md` (approved).

## Global Constraints

- `uv` only — never pip/venv. All commands `uv run ...`.
- **Bare exit codes** — never gate on `cmd | tail`/`| grep`; run bare, check `$?` or `&&`.
- Pyright strict 0 errors across `src/` and `tests/`. `LogRecord` extras via `setattr`/`getattr`, not attribute access, to stay strict-clean.
- **PostToolUse formatter strips not-yet-used imports** — add an import and its first use in the SAME edit.
- TDD is binding and audited: failing tests first, RUN them, quote red output in the report.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No backticks in double-quoted `git commit -m`.
- Commit ONLY listed files — never `git add -A`.
- Postgres: `docker compose up -d db` (container `marketplace-db-1`; `sg docker -c "..."` fallback). URL `postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace`.
- **Access-log lines never carry headers, bodies, or query strings** (tokens/reset-tokens live there) — method, path, status, duration_ms, request_id only. Security boundary.
- **PENDING outbox rows are never deleted at any age.**
- Migrations render plain types, no app-internal imports. Chain after this branch: exactly 10.
- Work on branch `obs-ops` (created before Task 1; Step 0 skipped by implementers).

---

### Task 1: observability.py — logging config, request-id middleware, access log, error envelope

**Files:**
- Create: `src/marketplace/observability.py`
- Modify: `src/marketplace/settings.py` (one field after `mail_from`)
- Modify: `src/marketplace/api.py` (import + `configure_logging()` call + `add_middleware` at app assembly ~line 1967)
- Test: `tests/test_observability.py` (new)

**Interfaces:**
- Consumes: `settings` (pydantic-settings singleton), existing `IdempotencyMiddleware` mount at api.py:1968.
- Produces (later tasks rely on): `observability.request_id_var: ContextVar[str]` (default `"-"`), `observability.configure_logging()` (idempotent), `observability.RequestIdMiddleware`, logger name `"marketplace.access"`, response/inbound header `X-Request-ID` (sanitize: ≤64 chars, printable ASCII, else regenerate `uuid4().hex`), envelope body exactly `{"detail": "internal error", "request_id": <id>}`. Task 4 adds its middleware BETWEEN the Idempotency and RequestId `add_middleware` lines.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_observability.py`:

```python
"""Request ids, access log, JSON formatter, and the 500 envelope."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from marketplace import api
from marketplace.observability import JsonFormatter, request_id_var
from tests.conftest import AuthFactory


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/healthz")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)


def test_inbound_request_id_honored(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("bad", ["x" * 65, "bad\nnewline", "smuggl\x00null"])
def test_hostile_request_id_regenerated(client: TestClient, bad: str) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": bad})
    assert r.headers["x-request-id"] != bad
    assert len(r.headers["x-request-id"]) == 32


def test_access_log_line_shape_and_redaction(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="marketplace.access"):
        client.get("/v1/service_types?secret=topsecret", headers={"X-Request-ID": "rid-1"})
    records = [r for r in caplog.records if r.name == "marketplace.access"]
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "method") == "GET"
    assert getattr(rec, "path") == "/v1/service_types"  # no query string, ever
    assert isinstance(getattr(rec, "status"), int)
    assert getattr(rec, "duration_ms") >= 0
    assert request_id_var.get() != ""  # contextvar machinery alive
    assert "topsecret" not in rec.getMessage()


def test_healthz_not_access_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="marketplace.access"):
        client.get("/healthz")
    assert not [r for r in caplog.records if r.name == "marketplace.access"]


def test_json_formatter_shape() -> None:
    fmt = JsonFormatter()
    rec = logging.LogRecord("marketplace.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    line = json.loads(fmt.format(rec))
    assert line["msg"] == "hello world"
    assert line["level"] == "INFO"
    assert line["logger"] == "marketplace.test"
    assert "ts" in line and "request_id" in line


def test_envelope_hides_internals(caplog: pytest.LogCaptureFixture) -> None:
    """An unhandled exception becomes a clean 500 with the request id; the
    traceback goes to the log, never the body."""

    @api.app.get("/v1/_test_boom")
    def _boom() -> None:
        raise RuntimeError("kaboom-sentinel")

    try:
        with caplog.at_level(logging.ERROR, logger="marketplace"):
            c = TestClient(api.app)
            r = c.get("/v1/_test_boom", headers={"X-Request-ID": "boom-rid"})
        assert r.status_code == 500
        assert r.json() == {"detail": "internal error", "request_id": "boom-rid"}
        assert r.headers["x-request-id"] == "boom-rid"
        assert "kaboom-sentinel" not in r.text
        logged = "".join(
            (r.message or "") + (str(r.exc_text) if r.exc_text else "") for r in caplog.records
        )
        assert "kaboom-sentinel" in logged  # traceback IS in the log
    finally:
        api.app.router.routes[:] = [
            route for route in api.app.router.routes if getattr(route, "path", "") != "/v1/_test_boom"
        ]


def test_http_exceptions_not_enveloped(client: TestClient, auth: AuthFactory) -> None:
    """404s/422s keep their FastAPI shapes; only unhandled errors are enveloped."""
    r = client.get("/v1/nope-does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] != "internal error"
    assert "x-request-id" in r.headers
```

Adaptation notes: `GET /v1/service_types` — verify a cheap unauthenticated GET exists (any stable route works; keep the query-string redaction assertion). If `caplog` misses records because dictConfig set `propagate=False` anywhere on the `marketplace.*` tree, root propagation must stay ON for app loggers (only `uvicorn.*` gets `propagate: False`) — that is a requirement, not a test bug.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_observability.py -v
```

Expected: ImportError (`marketplace.observability` doesn't exist). Capture output.

- [ ] **Step 3: Implement — settings.py**

After `mail_from`:

```python
    # Observability. JSON logs by default; LOG_FORMAT=plain for dev readability.
    log_format: str = "json"
```

- [ ] **Step 4: Implement — observability.py**

```python
"""Request-boundary observability: logging config, request ids, access log,
error envelope.

One middleware owns the whole boundary: it stamps a request id into a
contextvar (so every app log line carries it), emits exactly one access-log
line per request, and converts any escaped exception into a clean 500
envelope — traceback to the log, never the response. A separate
@app.exception_handler would run OUTSIDE user middleware (Starlette's
ServerErrorMiddleware), losing the header and the contextvar: keeping the
envelope here is load-bearing, not style.

Access lines never carry headers, bodies, or query strings — tokens ride in
headers and reset tokens in bodies. Method, path, status, duration, id. Only.
"""

import json
import logging
import logging.config
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .settings import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_MAX_REQUEST_ID = 64
_ACCESS_FIELDS = ("method", "path", "status", "duration_ms")

access_logger = logging.getLogger("marketplace.access")
error_logger = logging.getLogger("marketplace")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key in _ACCESS_FIELDS:
            value = record.__dict__.get(key)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging() -> None:
    """Process-wide logging: one handler, one format, uvicorn folded in.

    Idempotent — dictConfig replaces handlers wholesale. App loggers keep
    propagating to root (caplog and tests rely on it); only uvicorn's own
    loggers are detached, and its access line is silenced in favor of ours.
    """
    plain = settings.log_format == "plain"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "plain": {
                    "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
                },
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "plain" if plain else "json",
                    "filters": ["request_id"],
                }
            },
            "root": {"handlers": ["stderr"], "level": "INFO"},
            "loggers": {
                "uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            },
        }
    )


def _clean_request_id(raw: str | None) -> str:
    if raw and len(raw) <= _MAX_REQUEST_ID and raw.isascii() and raw.isprintable():
        return raw
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Request id + access log + error envelope. Mount OUTERMOST (add last)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _clean_request_id(Headers(scope=scope).get("x-request-id"))
        request_id_var.set(rid)
        method = str(scope.get("method", "-"))
        path = str(scope["path"])
        start = time.monotonic()
        status = 0
        started = False

        async def send_with_header(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = int(message["status"])
                message.setdefault("headers", []).append((b"x-request-id", rid.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            error_logger.exception("unhandled error on %s %s", method, path)
            status = 500
            if not started:
                response = JSONResponse(
                    {"detail": "internal error", "request_id": rid},
                    status_code=500,
                    headers={"x-request-id": rid},
                )
                await response(scope, receive, send)
            # response already streaming: nothing safe to send; the log has it
        finally:
            if path != "/healthz":
                access_logger.info(
                    "%s %s %s",
                    method,
                    path,
                    status,
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.monotonic() - start) * 1000, 1),
                    },
                )
```

Note: pyright strict — `record.request_id = ...` inside `RequestIdFilter` is a dynamic attribute on `LogRecord`; if pyright objects, use `setattr(record, "request_id", request_id_var.get())` and keep `hasattr` as the guard (do NOT clobber an explicit extra).

- [ ] **Step 5: Implement — api.py wiring**

At the app-assembly block (~line 1966), in the same edit (formatter strips unused imports):

```python
from .observability import RequestIdMiddleware, configure_logging  # goes in the import block

configure_logging()
app = FastAPI(title="Marketplace", version="1.0.0", lifespan=_lifespan)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RequestIdMiddleware)  # added last = outermost; Task 4 inserts between
```

(`configure_logging()` sits immediately above the `app = FastAPI(...)` line.)

- [ ] **Step 6: Run tests, then everything — bare exit codes**

```bash
uv run pytest tests/test_observability.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0. If any existing test asserted on log formatting, fix the test (format changed by design — list every such edit in the report).

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/observability.py src/marketplace/settings.py src/marketplace/api.py tests/test_observability.py
git commit -m "Add request-id logging, JSON access log, and 500 error envelope"
```

---

### Task 2: GET /v1/admin/stats

**Files:**
- Modify: `src/marketplace/models.py` (two schemas after `MarginSummaryOut` ~line 440)
- Modify: `src/marketplace/api.py` (`_STARTED_AT` near `logger` ~line 135; `_count_by` helper near `_paginate` ~line 322; endpoint after `margins_summary` ~line 1740)
- Test: `tests/test_stats.py` (new)

**Interfaces:**
- Consumes: existing entities/enums (`Job`/`JobStatus`, `Payment`/`PaymentStatus`, `Payout`/`PayoutStatus`, `Notification`/`NotificationStatus`, `Dispute`/`DisputeStatus.OPEN`, `Report`/`ReportStatus.OPEN`, `User`/`UserRole`/`UserStatus`, `Quote`, `IdempotencyRecord`, `WebhookEvent`), `_now()`, `admin_router`, `AdminId`, `SessionDep`.
- Produces: `GET /v1/admin/stats` → `StatsOut`; `models.NotificationStats`; `api._STARTED_AT` (Task 5's demo asserts `uptime_seconds >= 0`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats.py`:

```python
"""Admin stats endpoint: the operator's one-call snapshot."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail


def test_stats_empty_db_full_enum_keys(client: TestClient, admin: Header) -> None:
    s = client.get("/v1/admin/stats", headers=admin).json()
    assert s["jobs"] == {
        "pending": 0, "awaiting_payment": 0, "accepted": 0,
        "completed": 0, "expired": 0, "cancelled": 0,
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
    r = client.post("/v1/admin/users/alice/suspend", headers=admin)
    assert r.status_code == 200, r.text
    s = client.get("/v1/admin/stats", headers=admin).json()
    assert s["users"]["suspended"] == 1
```

Adaptation notes: `new_job(...)` returns the job dict (`job["id"]`); the suspend route shape must mirror what `tests/test_moderation.py` actually calls (403-vs-401 for the admin-only assertion likewise — mirror the existing admin-only test idiom). Intents binding: full enum keys on empty DB, exact counts after one completed flow, suspended breakdown, admin-gated.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_stats.py -v
```

Expected: 404s (route missing) / KeyErrors. Capture.

- [ ] **Step 3: Implement — models.py**

After `MarginSummaryOut`:

```python
class NotificationStats(BaseModel):
    pending: int
    sent: int
    failed: int
    oldest_pending_age_seconds: float | None


class StatsOut(BaseModel):
    jobs: dict[str, int]
    payments: dict[str, int]
    payouts: dict[str, int]
    notifications: NotificationStats
    disputes_open: int
    reports_open: int
    users: dict[str, int]
    quotes_live: int
    retention: dict[str, int]
    uptime_seconds: int
```

- [ ] **Step 4: Implement — api.py**

Near `logger` (~line 135): `_STARTED_AT = _now()` — but `_now` is defined later in the file; place it wherever `_now` is already visible (right after `_now`'s definition is fine; verify).

Helper near `_paginate`:

```python
def _count_by(session: Session, column: Any, enum: type[StrEnum]) -> dict[str, int]:
    """Group-count a status column; every enum member present, absent = 0."""
    rows = dict(session.execute(select(column, func.count()).group_by(column)).all())
    return {m.value: int(rows.get(m, 0)) for m in enum}
```

(`StrEnum` import from `enum` — same-edit rule. If the ORM returns raw strings on one backend, normalize: `rows.get(m, rows.get(m.value, 0))`.)

Endpoint after `margins_summary`:

```python
@admin_router.get("/stats", response_model=StatsOut)
def admin_stats(session: SessionDep, admin_id: AdminId) -> StatsOut:
    """Operator snapshot: counts only, one session, curl-able."""
    oldest_pending = session.scalar(
        select(func.min(Notification.created_at)).where(
            Notification.status == NotificationStatus.PENDING
        )
    )
    users = _count_by(session, User.role, UserRole)
    users["suspended"] = session.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.SUSPENDED)
    ) or 0
    return StatsOut(
        jobs=_count_by(session, Job.status, JobStatus),
        payments=_count_by(session, Payment.status, PaymentStatus),
        payouts=_count_by(session, Payout.status, PayoutStatus),
        notifications=NotificationStats(
            pending=_count_by(session, Notification.status, NotificationStatus)["pending"],
            sent=_count_by(session, Notification.status, NotificationStatus)["sent"],
            failed=_count_by(session, Notification.status, NotificationStatus)["failed"],
            oldest_pending_age_seconds=(
                (_now() - oldest_pending).total_seconds() if oldest_pending else None
            ),
        ),
        disputes_open=session.scalar(
            select(func.count()).select_from(Dispute).where(Dispute.status == DisputeStatus.OPEN)
        ) or 0,
        reports_open=session.scalar(
            select(func.count()).select_from(Report).where(Report.status == ReportStatus.OPEN)
        ) or 0,
        users=users,
        quotes_live=session.scalar(
            select(func.count()).select_from(Quote).where(Quote.expires_at > _now())
        ) or 0,
        retention={
            "idempotency_keys": session.scalar(
                select(func.count()).select_from(IdempotencyRecord)
            ) or 0,
            "webhook_events": session.scalar(select(func.count()).select_from(WebhookEvent)) or 0,
            "notifications_total": session.scalar(
                select(func.count()).select_from(Notification)
            ) or 0,
        },
        uptime_seconds=int((_now() - _STARTED_AT).total_seconds()),
    )
```

Obvious cleanup while implementing: compute `_count_by(session, Notification.status, NotificationStatus)` ONCE into a local and index it three times — do that, the triple call above is illustrative shorthand.

- [ ] **Step 5: Run tests, then everything**

```bash
uv run pytest tests/test_stats.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py tests/test_stats.py
git commit -m "Add admin stats endpoint: one-call operator snapshot"
```

---

### Task 3: Retention sweeps + webhook off the event loop + PG race test

**Files:**
- Modify: `src/marketplace/settings.py` (three fields after `log_format`)
- Modify: `src/marketplace/api.py` (`_sweep_retention` after `_sweep_expired_auth` ~line 282; `_sweep` gains a line ~line 288; `payments_webhook` rewrite ~line 1928)
- Test: `tests/test_retention.py` (new); PG race test appended to `tests/test_payments.py`

**Interfaces:**
- Consumes: `IdempotencyRecord.created_at`, `WebhookEvent.received_at`, `Notification.created_at`/`.status` (`NotificationStatus.SENT/FAILED/PENDING`), `_sweep`, `SessionLocal`, `_apply_payment_event(session, event)`, `PaymentEvent`, `asyncio` (already imported in api.py).
- Produces: `api._sweep_retention(session)`; `api._process_webhook(event) -> dict[str, str]` (sync, own session); settings `retention_idempotency_days=7`, `retention_webhooks_days=30`, `retention_notifications_days=30`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retention.py`:

```python
"""Retention sweeps: bounded tables, immortal PENDING outbox rows."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import IdempotencyRecord, Notification, WebhookEvent
from marketplace.models import EventKind, NotificationStatus


def _days_ago(n: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)


def _seed(session, *, idem_age: float, hook_age: float, note_age: float,
          note_status: NotificationStatus) -> None:
    session.add(IdempotencyRecord(
        principal="buyer:alice", key=f"k-{idem_age}", path="/v1/jobs",
        response_status=200, response_body="{}", created_at=_days_ago(idem_age),
    ))
    session.add(WebhookEvent(
        provider_event_id=f"evt-{hook_age}", kind="payment_succeeded",
        received_at=_days_ago(hook_age),
    ))
    session.add(Notification(
        user_id="alice", email="a@example.com", kind=EventKind.JOB_ACCEPTED_BUYER,
        status=note_status, created_at=_days_ago(note_age),
    ))


def test_retention_reaps_old_keeps_young(client: TestClient, auth) -> None:
    auth("buyer", "alice")  # FK target for notifications
    with SessionLocal() as s:
        _seed(s, idem_age=8, hook_age=31, note_age=31, note_status=NotificationStatus.SENT)
        _seed(s, idem_age=6, hook_age=29, note_age=29, note_status=NotificationStatus.SENT)
        s.commit()
    with SessionLocal() as s:
        api._sweep_retention(s)
        s.commit()
    with SessionLocal() as s:
        assert len(s.scalars(select(IdempotencyRecord)).all()) == 1
        assert len(s.scalars(select(WebhookEvent)).all()) == 1
        assert len(s.scalars(select(Notification)).all()) == 1


def test_pending_outbox_rows_are_immortal(client: TestClient, auth) -> None:
    auth("buyer", "alice")
    with SessionLocal() as s:
        _seed(s, idem_age=1, hook_age=1, note_age=400, note_status=NotificationStatus.PENDING)
        _seed(s, idem_age=1, hook_age=1, note_age=400, note_status=NotificationStatus.FAILED)
        s.commit()
    with SessionLocal() as s:
        api._sweep_retention(s)
        s.commit()
    with SessionLocal() as s:
        kept = s.scalars(select(Notification)).all()
        assert [n.status for n in kept] == [NotificationStatus.PENDING]


def test_retention_sweep_is_idempotent(client: TestClient, auth) -> None:
    auth("buyer", "alice")
    with SessionLocal() as s:
        _seed(s, idem_age=8, hook_age=31, note_age=31, note_status=NotificationStatus.FAILED)
        s.commit()
    for _ in range(2):
        with SessionLocal() as s:
            api._sweep_retention(s)
            s.commit()
    with SessionLocal() as s:
        assert s.scalars(select(IdempotencyRecord)).all() == []
```

Type the `auth` params properly (`AuthFactory` from `tests.conftest`). If `EventKind.JOB_ACCEPTED_BUYER` isn't the exact member name, pick any real member — the kind is irrelevant to retention.

Append to `tests/test_payments.py` (mirror the file's existing PG-gated concurrency idiom — `IS_POSTGRES`, `threading.Barrier`, `ThreadPoolExecutor`, fresh `TestClient` per thread):

```python
@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_cancel_vs_webhook_race(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Admin cancel of an AWAITING_PAYMENT job races the payment_succeeded
    webhook. Exactly one side wins; the pair (job.status, payment.status) must
    land consistent: (cancelled, FAILED) — cancel won, charge voided;
    (accepted, SUCCEEDED) — webhook won, cancel lost/409d; or
    (cancelled, REFUNDED) — webhook won, then cancel refunded the paid charge.
    NEVER (accepted, FAILED) or (cancelled, SUCCEEDED)."""
    onboard_and_avail(client, auth, basic_service, "s1")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    job = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    with SessionLocal() as s:
        ppid = s.scalar(select(Payment.provider_payment_id).where(Payment.job_id == UUID(job["id"])))

    barrier = threading.Barrier(2)

    def do_cancel() -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin).status_code

    def do_webhook() -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post("/v1/payments/webhook", json={
            "event_id": "evt-race-1", "kind": "payment_succeeded", "object_id": ppid,
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(do_cancel), pool.submit(do_webhook)
        codes = (f1.result(), f2.result())
    assert all(code in (200, 409) for code in codes), codes

    with SessionLocal() as s:
        final_job = s.get(Job, UUID(job["id"]))
        payment = s.scalar(select(Payment).where(Payment.job_id == UUID(job["id"])))
        assert final_job is not None and payment is not None
        pair = (final_job.status, payment.status)
    assert pair in (
        (JobStatus.CANCELLED, PaymentStatus.FAILED),
        (JobStatus.ACCEPTED, PaymentStatus.SUCCEEDED),
        (JobStatus.CANCELLED, PaymentStatus.REFUNDED),
    ), pair
```

Adapt the fake-webhook payload shape to whatever the existing webhook tests post (copy their idiom exactly); admin cancel may return other codes for lost races (e.g. 502 if the void hits a settled charge) — if the run shows one, widen `codes` only with justification in the report, never the final-pair invariant.

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_retention.py -v
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_payments.py::test_cancel_vs_webhook_race -v
```

Expected: retention tests fail on missing `_sweep_retention`; the race test may PASS already (it tests existing locking — that's fine, it's a regression net; note it in the report either way). Capture output.

- [ ] **Step 3: Implement — settings + _sweep_retention**

settings.py, after `log_format`:

```python
    # Retention (days): swept tables stay bounded. PENDING outbox rows are
    # never reaped regardless of age.
    retention_idempotency_days: int = 7
    retention_webhooks_days: int = 30
    retention_notifications_days: int = 30
```

api.py, after `_sweep_expired_auth`:

```python
def _sweep_retention(session: Session) -> None:
    """Bounded tables: reap replay/dedup rows and delivered mail past their
    windows. PENDING outbox rows are immortal — the outbox contract holds."""
    now = _now()
    session.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.created_at < now - timedelta(days=settings.retention_idempotency_days)
        )
    )
    session.execute(
        delete(WebhookEvent).where(
            WebhookEvent.received_at < now - timedelta(days=settings.retention_webhooks_days)
        )
    )
    session.execute(
        delete(Notification).where(
            Notification.status.in_((NotificationStatus.SENT, NotificationStatus.FAILED)),
            Notification.created_at < now - timedelta(days=settings.retention_notifications_days),
        )
    )
```

And in `_sweep(...)` add `_sweep_retention(session)` after `_sweep_expired_auth(session)`. Add `IdempotencyRecord` to api.py's entities import in the same edit.

- [ ] **Step 4: Implement — webhook offload**

Replace `payments_webhook` (~line 1928):

```python
def _process_webhook(event: PaymentEvent) -> dict[str, str]:
    """Dedup + apply on a worker thread — sync Session stays off the loop."""
    with SessionLocal() as session:
        duplicate = session.scalar(
            select(WebhookEvent).where(WebhookEvent.provider_event_id == event.event_id)
        )
        if duplicate is not None:
            return {"status": "duplicate"}
        session.add(WebhookEvent(provider_event_id=event.event_id, kind=event.kind))
        _apply_payment_event(session, event)
        session.commit()
    return {"status": "ok"}


@payments_router.post("/webhook")
async def payments_webhook(request: Request, provider: ProviderDep) -> dict[str, str]:
    """Provider event sink. Unauthenticated by design — authenticity comes from
    the provider's signature, verified in parse_webhook. Duplicates no-op."""
    payload = await request.body()
    try:
        event = provider.parse_webhook(payload, request.headers.get("stripe-signature"))
    except WebhookSignatureError:
        raise HTTPException(status_code=400, detail="invalid webhook signature") from None
    except (PaymentError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed webhook payload") from None
    return await asyncio.to_thread(_process_webhook, event)
```

Verify against the actual decorator/router name for the current handler and mirror how sibling `SessionLocal()` users commit (e.g. `_run_sweep_once`) — if the house idiom is `with SessionLocal() as s, s.begin():`, use that and drop the explicit `commit()`. The dropped `SessionDep` must not leave an unused import.

- [ ] **Step 5: Run everything**

```bash
uv run pytest tests/test_retention.py tests/test_payments.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0 — the existing webhook suite green is the offload's behavior-preservation proof; say so explicitly in the report.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/settings.py src/marketplace/api.py tests/test_retention.py tests/test_payments.py
git commit -m "Add retention sweeps, move webhook DB work off the event loop"
```

---

### Task 4: API hardening — body cap, TrustedHost/CORS, pagination, migration #10

**Files:**
- Modify: `src/marketplace/observability.py` (BodySizeLimitMiddleware at the end)
- Modify: `src/marketplace/settings.py` (three fields after the retention block)
- Modify: `src/marketplace/api.py` (`_add_hardening_middleware` + call between the two existing `add_middleware` lines; pagination on three admin lists)
- Modify: `src/marketplace/entities.py` (`index=True` on `SellerProfile.provider_account_id` ~line 133 and `Payout.provider_transfer_id` ~line 325)
- Create: `migrations/versions/<autogen>_ops_indexes.py`
- Test: `tests/test_hardening.py` (new); pagination asserts appended to `tests/test_moderation.py` or wherever the three lists are already exercised (implementer's call — mirror existing list tests)

**Interfaces:**
- Consumes: Task 1's `observability.py` module and middleware ordering contract; `Limit`/`Offset`/`_paginate` (api.py:144-145, 322); migration head `ce07e913bc82`... actually head is migration #9; run `uv run alembic heads` and use THAT hash as down_revision.
- Produces: `observability.BodySizeLimitMiddleware`; `api._add_hardening_middleware(app: FastAPI) -> None`; settings `trusted_hosts: list[str] = ["*"]`, `cors_origins: list[str] = []`, `max_body_bytes: int = 1_048_576`; paginated `/v1/admin/reviews/{kind}`, `/v1/admin/reports`, `/v1/admin/buyers`; migration #10 (two indexes).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hardening.py`:

```python
"""Body-size cap, TrustedHost/CORS wiring, admin-list pagination."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketplace import api
from marketplace.settings import settings
from tests.conftest import AuthFactory, Header


def test_oversized_body_413(client: TestClient, auth: AuthFactory) -> None:
    big = "x" * (settings.max_body_bytes + 1)
    r = client.post(
        "/v1/quotes", content=big.encode(),
        headers={**auth("buyer", "alice"), "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "request body too large"


def test_normal_body_passes(client: TestClient, auth: AuthFactory, basic_service: str) -> None:
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 200


def test_oversized_413_not_stored_for_replay(client: TestClient, auth: AuthFactory) -> None:
    """The cap sits OUTSIDE idempotency: a 413 must not be replayable."""
    big = "x" * (settings.max_body_bytes + 1)
    headers = {**auth("buyer", "alice"), "Idempotency-Key": "cap-key-1",
               "Content-Type": "application/json"}
    assert client.post("/v1/quotes", content=big.encode(), headers=headers).status_code == 413
    from marketplace.db import SessionLocal
    from marketplace.entities import IdempotencyRecord
    from sqlalchemy import select
    with SessionLocal() as s:
        assert s.scalars(select(IdempotencyRecord).where(IdempotencyRecord.key == "cap-key-1")).all() == []


def test_default_host_and_cors_are_open_and_absent(client: TestClient) -> None:
    r = client.get("/healthz", headers={"Host": "anything.example"})
    assert r.status_code == 200          # trusted_hosts defaults to *
    assert "access-control-allow-origin" not in r.headers  # CORS off by default


def test_hardening_wiring_respects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """_add_hardening_middleware reads settings: CORS only when configured,
    TrustedHost enforced when narrowed."""
    scratch = FastAPI()

    @scratch.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "yes"}

    monkeypatch.setattr(settings, "cors_origins", ["https://app.example"])
    monkeypatch.setattr(settings, "trusted_hosts", ["good.example"])
    api._add_hardening_middleware(scratch)
    c = TestClient(scratch, base_url="http://good.example")
    r = c.get("/ping", headers={"Origin": "https://app.example"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example"
    assert TestClient(scratch).get("/ping", headers={"Host": "evil.example"}).status_code == 400


def test_admin_lists_paginate(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    a1 = auth("buyer", "alice")
    a2 = auth("buyer", "bob")
    client.get("/v1/profile", headers=a1)
    client.get("/v1/profile", headers=a2)  # materialize two buyer profiles
    full = client.get("/v1/admin/buyers", headers=admin).json()
    assert len(full) >= 2
    page = client.get("/v1/admin/buyers?limit=1", headers=admin).json()
    assert len(page) == 1
    page2 = client.get("/v1/admin/buyers?limit=1&offset=1", headers=admin).json()
    assert page2 and page2[0] != page[0]
    # reviews + reports accept the params too (empty lists are fine)
    assert client.get("/v1/admin/reviews/buyer?limit=1", headers=admin).status_code == 200
    assert client.get("/v1/admin/reports?limit=1", headers=admin).status_code == 200
```

Adaptation notes: `/v1/profile` GET may auto-create the buyer profile (write-on-first-GET) — if not, materialize profiles the way existing buyer tests do. The oversized-body tests use `/v1/quotes` for a real authenticated POST; any body-accepting route works.

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_hardening.py -v
```

Expected: 413 tests fail (body passes through today), wiring test fails on missing `_add_hardening_middleware`, pagination params ignored (422 or full list). Capture.

- [ ] **Step 3: Implement — settings + BodySizeLimitMiddleware**

settings.py after the retention block:

```python
    # API hardening. trusted_hosts/* and empty cors_origins = open (dev);
    # narrow both in production. Bodies over max_body_bytes get a 413.
    trusted_hosts: list[str] = ["*"]
    cors_origins: list[str] = []
    max_body_bytes: int = 1_048_576
```

observability.py, at the end:

```python
class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """413 oversized requests: declared Content-Length up front, and a
    counted cap on chunked bodies. Mount OUTSIDE IdempotencyMiddleware so an
    oversized 413 is never stored for replay."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = settings.max_body_bytes
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send)
            return
        seen = 0

        async def receive_capped() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, receive_capped, send)
        except _BodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": "request body too large"}, status_code=413)
        await response(scope, receive, send)
```

(Known edge, accepted: a chunked-body 413 after the response started can't be delivered — same limitation Starlette's own error middleware has. `# ponytail:` comment it.)

- [ ] **Step 4: Implement — api.py wiring + pagination**

Between the existing two `add_middleware` lines:

```python
def _add_hardening_middleware(app: FastAPI) -> None:
    """Settings-driven: CORS only when origins are configured; TrustedHost
    defaults open (*) until narrowed; body cap always on."""
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(BodySizeLimitMiddleware)


app.add_middleware(IdempotencyMiddleware)
_add_hardening_middleware(app)
app.add_middleware(RequestIdMiddleware)  # outermost, unchanged
```

Imports in the same edit: `from fastapi.middleware.cors import CORSMiddleware`, `from starlette.middleware.trustedhost import TrustedHostMiddleware`, and `BodySizeLimitMiddleware` added to the existing `.observability` import line.

Pagination — three endpoints get the exact sibling idiom (`limit: Limit = 100, offset: Offset = 0`, `return _paginate(rows, limit, offset)`):
- `admin_list_reviews` (api.py:1345): paginate the `rows` list before mapping to `_admin_review_out`.
- `admin_list_reports` (api.py:1404): wrap the `.all()` list.
- `admin_list_buyers` (api.py:1437): wrap the `.all()` list.

- [ ] **Step 5: Implement — indexes + migration #10**

entities.py: add `index=True` to `SellerProfile.provider_account_id` and `Payout.provider_transfer_id` (keep other kwargs).

```bash
uv run alembic heads   # confirm current head (migration #9) — use it as down_revision
uv run alembic revision -m "ops indexes"
```

Fill (style-match the newest migration file; plain types, no app imports):

```python
def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("ix_seller_profiles_provider_account_id"),
        "seller_profiles",
        ["provider_account_id"],
    )
    op.create_index(
        op.f("ix_payouts_provider_transfer_id"), "payouts", ["provider_transfer_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_payouts_provider_transfer_id"), table_name="payouts")
    op.drop_index(op.f("ix_seller_profiles_provider_account_id"), table_name="seller_profiles")
```

- [ ] **Step 6: Run everything**

```bash
uv run pytest tests/test_hardening.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/observability.py src/marketplace/settings.py src/marketplace/api.py src/marketplace/entities.py migrations/versions/ tests/test_hardening.py
git commit -m "Add body cap, TrustedHost/CORS wiring, admin-list pagination, ops indexes"
```

---

### Task 5: Demo act 8, docs, full merge gates

**Files:**
- Modify: `scripts/demo.py` (act 8 after step 21, before the final print; final print extended)
- Modify: `ROADMAP.md`, `README.md`, `SECURITY.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: demo proves stats + request-id round-trip; docs record the observability bucket done and the template feature-complete.

- [ ] **Step 1: Add the demo act**

After step 21's block:

```python
    # --- Act 8: observability (the operator's view of everything above) ---
    print("22. Ops: admin stats snapshot + request-id round-trip")
    r = c.get("/v1/admin/stats", headers={**admin, "X-Request-ID": "demo-run-123"})
    assert r.status_code == 200, r.text
    assert r.headers["x-request-id"] == "demo-run-123"
    stats = r.json()
    assert stats["jobs"]["completed"] >= 2, stats["jobs"]
    assert stats["payments"]["succeeded"] >= 1, stats["payments"]
    assert stats["notifications"]["pending"] >= 0
    assert stats["uptime_seconds"] >= 0
    print(
        f"   jobs={stats['jobs']}  payments={stats['payments']}\n"
        f"   outbox pending={stats['notifications']['pending']}  "
        f"retention rows={stats['retention']}"
    )
```

Extend the final summary print to end with: `"..., margin reported net of provider fees, ops snapshot live with request-id tracing."`

Sanity-check the `>= 2` completed count against the actual demo flow (acts 1-7 complete at least jobs 1 and 2); adjust the bound to what the run shows, keeping it a real assertion (never `>= 0` for jobs/payments).

- [ ] **Step 2: Run the demo**

```bash
uv run python scripts/demo.py
```

Expected: exit 0, 22 steps.

- [ ] **Step 3: Update the docs**

- `ROADMAP.md`: observability item → done: an "**Observability & ops (done):**" paragraph under "Where we are" (style-match the fee-aware-margin one: request-id JSON logging + plain toggle, 500 envelope, admin stats, 7/30/30 retention with immortal PENDING rows, webhook off the event loop, body cap + TrustedHost/CORS, pagination, indexes, migration #10, PG cancel-vs-webhook race test) + `Done ✓` entry. The "What's still ahead" list shrinks to admin RBAC, gateway rate-limiting/API extras, OAuth — reframed with one sentence: these are fork work by decision (Danny, 2026-07-15); **the template is feature-complete**.
- `SECURITY.md`: new section — envelope guarantee (unhandled errors return `{"detail": "internal error", "request_id"}`, tracebacks only in logs); access-log redaction stance (no headers/bodies/query strings, and why); retention windows + the stale-webhook-replay note (a >30d replay re-applies against terminal-guarded state transitions → no-ops at the state machine); body cap default 1 MiB; TrustedHost/CORS defaults open-for-dev, narrow in prod; the idempotency secret-echo standing rule (auth exclusion tested; `client_secret` in accept responses is buyer-facing by design, per-principal scoped).
- `README.md`: stats endpoint in the admin list; a short "Running it" ops note: `X-Request-ID` honored/echoed, `LOG_FORMAT=plain`, the new env vars (`TRUSTED_HOSTS`, `CORS_ORIGINS`, `MAX_BODY_BYTES`, `RETENTION_*_DAYS`) wherever existing env vars are documented.
- `CLAUDE.md`: migration total 9 → 10; three new non-negotiable bullets: access logs never carry headers/bodies/query strings; PENDING outbox rows are never reaped; webhook DB work stays off the event loop (`_process_webhook` via `asyncio.to_thread`). Check the "Explicit non-goals" paragraph for residual contradictions (observability was never listed there, but VERIFY — last two branches both had a stale-docs finding).

- [ ] **Step 4: Full gates — bare exit codes, both backends, fresh-volume migrations**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
uv run python scripts/demo.py
docker compose down -v && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: every command exits 0; exactly 10 migrations from scratch; PG suite green on the fresh volume.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py ROADMAP.md README.md SECURITY.md CLAUDE.md
git commit -m "Document observability: demo act 8, template feature-complete"
```

---

## Self-review notes (already applied)

- Spec coverage: §1+§2 (logging/request-id/envelope, one middleware) → Task 1; §3 (stats) → Task 2; §4 (retention) + §5 (webhook offload) + §7 (race test) → Task 3; §6 (hardening: cap/hosts/CORS/pagination/indexes) → Task 4; §7 secret-echo audit → final-review dispatch instruction + Task 5 SECURITY.md text; §8 (demo/docs) → Task 5. Non-goals untouched.
- The envelope-inside-middleware decision is restated at every point of contact (spec §2, Task 1 module docstring, architecture note) because it is the one thing a well-meaning refactor to `@app.exception_handler` would silently break — Task 1's `test_envelope_hides_internals` discriminates it (the header assert fails on the handler path).
- Middleware order contract is stated identically in Task 1 (RequestId added last) and Task 4 (hardening inserted between) — add-order: Idempotency, [CORS, TrustedHost, BodyCap], RequestId.
- Type consistency: `request_id_var`/`configure_logging`/`RequestIdMiddleware`/`BodySizeLimitMiddleware` names match across Tasks 1/4 and tests; `StatsOut`/`NotificationStats` field names identical in models/endpoint/tests/demo; retention setting names identical in settings/`_sweep_retention`/tests.
- Line anchors were verified 2026-07-15 but are hints — match on code snippets.
