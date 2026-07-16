# Commit Before Response Implementation Plan (F2 + F2b)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every write endpoint's DB commit complete before its response reaches the client (F2), and make the idempotency replay record durable before the response is sent (F2b) — with a real-uvicorn test harness, since TestClient is structurally blind to this bug class.

**Architecture:** A `CommitRoute(APIRoute)` subclass in `db.py` commits the request's session (stashed on `request.state` by `get_session`) after the handler returns its `Response` but before it reaches the sender — ordering by construction. `get_session` stops committing (teardown = close-only). All 7 routers get `route_class=CommitRoute`, enforced by an import-time invariant. `IdempotencyMiddleware` buffers → stores → sends. Red/green is made deterministic by a `slow_commits` fixture (150ms `Session.commit`) driven over a real socket.

**Tech Stack:** FastAPI `APIRoute`, `starlette.concurrency.run_in_threadpool`, uvicorn-in-a-thread test fixture, httpx, SQLAlchemy 2.0.

**Spec:** `docs/superpowers/specs/2026-07-16-commit-before-response-design.md` (approved).

## Global Constraints

- `uv` only. **Bare exit codes** — never gate on piped commands.
- Pyright strict 0 across `src/` and `tests/`; ruff clean. Repo precedent for unavoidable test-side suppressions: narrow `# type: ignore[...]` / `# pyright: ignore[...]` comments.
- **PostToolUse formatter strips not-yet-used imports** — add an import and its first use in the SAME edit.
- TDD binding and audited: failing tests first, RUN them, quote red output in the report.
- **The wrapper commit MUST go through `run_in_threadpool`** — a bare `session.commit()` in async code puts every write's commit on the event loop (the exact class the webhook offload fixed). Binding constraint.
- **Do NOT touch money-path locking** — F5's `populate_existing` fixes and all `with_for_update` code are out of scope; the existing PG race tests must pass unmodified.
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No backticks in double-quoted `git commit -m`. Commit ONLY listed files.
- Postgres: `postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace` (`docker compose up -d db`; `sg docker -c "..."` fallback).
- Branch `fix-commit-ordering` already exists with the spec committed — work there.
- NOTE: a dev server may be running on port 8000 from the try-out session; never bind it. The live fixture uses an ephemeral port.

---

### Task 1: CommitRoute + get_session rework + live-server fixture + invariant

**Files:**
- Modify: `src/marketplace/db.py` (CommitRoute; get_session rework)
- Modify: `src/marketplace/api.py` (6 routers gain `route_class=CommitRoute`: lines ~373, 456, 553, 829, 1206, 1920; `_assert_commit_routes` after the last `app.include_router(...)`)
- Modify: `src/marketplace/auth.py` (`auth_router` at line ~154 gains `route_class=CommitRoute`)
- Modify: `tests/conftest.py` (`live_server` + `slow_commits` fixtures)
- Test: `tests/test_live_server.py` (new)

**Interfaces:**
- Consumes: `SessionLocal`, existing `get_session` consumers (`SessionDep` in api.py, `_SessionDep` in auth.py — both `Depends(get_session)`, cached to one session/request; no direct callers exist, verified).
- Produces (Task 2 relies on): `db.CommitRoute`; `request.state.db_session`; conftest fixtures `live_server -> str` (base URL) and `slow_commits` (150ms commits); `api._assert_commit_routes(app)`.

- [ ] **Step 1: Write the failing tests**

conftest.py — append (hoist imports to the top block: `socket`, `threading`, `time`, `httpx`, `uvicorn`, `Iterator`, `Session` from sqlalchemy.orm):

```python
@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """A real uvicorn on a socket. TestClient awaits the full app cycle —
    dependency teardown included — so it is structurally blind to
    response-vs-commit ordering (finding F2); only a network client can see it.
    lifespan="off" is deliberate: the maintenance loop must not drain/sweep
    the shared test DB underneath other tests."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("live server failed to start")
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def slow_commits(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """150ms Session.commit + a timestamp of the last completed commit.
    Turns the response-vs-commit race into a deterministic assertion: pre-fix
    the client holds the response while the commit is still sleeping; post-fix
    responses are simply 150ms slower."""
    box: dict[str, float] = {}
    real_commit = Session.commit

    def timed_commit(self: Session) -> None:
        time.sleep(0.15)
        real_commit(self)
        box["last_commit_done"] = time.monotonic()

    monkeypatch.setattr(Session, "commit", timed_commit)
    return box
```

tests/test_live_server.py (new):

```python
"""F2 regression: commits must complete before the response reaches the
client. These tests talk to a REAL uvicorn over a socket — see the
live_server fixture for why TestClient cannot test this class."""

import time

import httpx

from tests.conftest import AuthFactory


def test_commit_lands_before_client_has_response(
    live_server: str, basic_service: str, auth: AuthFactory, slow_commits: dict[str, float]
) -> None:
    buyer = auth("buyer", "alice")
    with httpx.Client(base_url=live_server, timeout=10) as c:
        r = c.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
        received = time.monotonic()
    assert r.status_code == 200, r.text
    assert slow_commits["last_commit_done"] < received, (
        "the client held the response before its transaction committed (F2): "
        f"commit finished {slow_commits['last_commit_done'] - received:+.3f}s after receipt"
    )


def test_read_your_writes_over_socket(
    live_server: str, basic_service: str, auth: AuthFactory
) -> None:
    """The exact field repro shape: a fresh quote must be usable immediately
    (pre-fix this 404d ~2/30 on chained calls)."""
    buyer = auth("buyer", "alice")
    with httpx.Client(base_url=live_server, timeout=10) as c:
        for i in range(100):
            q = c.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
            assert q.status_code == 200, q.text
            j = c.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer)
            assert j.status_code == 200, f"iteration {i}: fresh quote invisible: {j.text}"
            c.post(f"/v1/jobs/{j.json()['id']}/cancel", headers=buyer)


def test_failed_commit_returns_500_envelope_and_persists_nothing(
    client: "TestClient", basic_service: str, auth: AuthFactory
) -> None:
    """A commit failure must be a truthful 500, never a lying 200 (in-process
    is fine here — failure semantics, not ordering, are under test)."""
    from fastapi import Request
    from sqlalchemy import func, select
    from sqlalchemy.exc import OperationalError

    from marketplace import api
    from marketplace.db import SessionLocal, get_session
    from marketplace.entities import Quote

    buyer = auth("buyer", "alice")

    def failing_get_session(request: Request):
        session = SessionLocal()
        request.state.db_session = session

        def boom() -> None:
            raise OperationalError("induced commit failure", None, Exception("induced"))

        session.commit = boom  # type: ignore[method-assign]
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[get_session] = failing_get_session
    try:
        r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    finally:
        api.app.dependency_overrides.clear()
    assert r.status_code == 500
    assert r.json()["detail"] == "internal error"
    assert "request_id" in r.json()
    assert "x-request-id" in r.headers
    with SessionLocal() as s:
        assert (s.scalar(select(func.count()).select_from(Quote)) or 0) == 0
```

Adaptation notes: fix the `TestClient` annotation import per house style; the induced-failure test relies on the envelope from `RequestIdMiddleware` (no `raise_server_exceptions` gymnastics needed — the middleware handles it). If pyright objects to the `Session.commit` monkeypatch or the instance-level `session.commit = boom`, use the repo's narrow-ignore precedent.

- [ ] **Step 2: Run to verify the RIGHT failures**

```bash
uv run pytest tests/test_live_server.py -v
```

Expected: `test_commit_lands_before_client_has_response` FAILS with a positive "+0.1xx s after receipt" delta (the deterministic F2 proof); `test_failed_commit_returns_500_envelope...` FAILS (today the lying 200 comes back — `request.state.db_session` isn't read by anything yet, and today's teardown commit is bypassed by the override, so expect a 200 with nothing persisted, or adapt the assert message accordingly — capture what actually happens); `test_read_your_writes_over_socket` MAY pass (100 iterations on SQLite may not hit the window — it's the regression net, not the proof; note the observed behavior either way). Quote the red output.

- [ ] **Step 3: Implement — db.py**

Replace `get_session` and add `CommitRoute` (imports in the same edit: `Any`, `Callable`, `Coroutine` from collections.abc/typing, `Request` from fastapi, `APIRoute` from fastapi.routing, `Response` from starlette.responses, `run_in_threadpool` from starlette.concurrency):

```python
def get_session(request: Request) -> Iterator[Session]:
    session = SessionLocal()
    request.state.db_session = session
    try:
        yield session
    finally:
        session.close()  # close() discards (rolls back) anything uncommitted


class CommitRoute(APIRoute):
    """Commits the request's DB session BEFORE the response object reaches
    the sender — never in dependency teardown, which runs after the response
    is on the wire (finding F2). Streaming responses would need a rethink;
    the app has none."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def commit_then_respond(request: Request) -> Response:
            response = await original(request)
            session: Session | None = getattr(request.state, "db_session", None)
            if session is not None and response.status_code < 400:
                # threadpool: a sync commit on the event loop would stall
                # every in-flight request (same class as the webhook offload)
                await run_in_threadpool(session.commit)
            return response

        return commit_then_respond
```

Update db.py's module docstring (it currently promises "committed on success ... in `get_session`") to describe the CommitRoute ordering.

- [ ] **Step 4: Implement — routers + invariant**

Every router gains the route class (import `CommitRoute` from `.db` in the same edit, both files):

```python
# api.py — all six:
reports_router = APIRouter(prefix="/v1", tags=["reports"], route_class=CommitRoute)
prefs_router = APIRouter(prefix="/v1", tags=["preferences"], route_class=CommitRoute)
buyer_router = APIRouter(prefix="/v1", tags=["buyer"], route_class=CommitRoute)
seller_router = APIRouter(prefix="/v1/seller", tags=["seller"], route_class=CommitRoute)
admin_router = APIRouter(
    prefix="/v1/admin", tags=["admin"],
    dependencies=[Depends(require_admin)], route_class=CommitRoute,
)
payments_router = APIRouter(prefix="/v1/payments", tags=["payments"], route_class=CommitRoute)

# auth.py:
auth_router = APIRouter(prefix="/v1/auth", tags=["auth"], route_class=CommitRoute)
```

api.py, immediately after the LAST `app.include_router(...)` line (`APIRoute` import from fastapi.routing in the same edit) — module level, so a missing route_class fails at import/test-collection, not just at boot:

```python
def _assert_commit_routes(app: FastAPI) -> None:
    """Every /v1 route must commit-before-response (finding F2); a plain
    APIRoute would silently regress to commit-in-teardown."""
    rogue = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and not isinstance(route, CommitRoute)
        and route.path.startswith("/v1")
    ]
    if rogue:
        raise RuntimeError(f"routes missing route_class=CommitRoute (finding F2): {rogue}")


_assert_commit_routes(app)
```

- [ ] **Step 5: Add the invariant unit test**

Append to tests/test_live_server.py:

```python
def test_commit_route_invariant_catches_rogue_router() -> None:
    from fastapi import APIRouter, FastAPI

    import pytest as _pytest
    from marketplace import api

    rogue_app = FastAPI()
    rogue = APIRouter(prefix="/v1")

    @rogue.get("/rogue")  # pyright: ignore[reportUnusedFunction]
    def rogue_route() -> dict[str, str]:
        return {}

    rogue_app.include_router(rogue)
    with _pytest.raises(RuntimeError, match="CommitRoute"):
        api._assert_commit_routes(rogue_app)
```

(Hoist imports per house style; `_pytest` shown only to avoid shadowing confusion — use plain `pytest` at top level.)

- [ ] **Step 6: Run tests, then everything — bare exit codes**

```bash
uv run pytest tests/test_live_server.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all green, both backends, INCLUDING the untouched PG race tests (locking unchanged). If any existing test breaks, the likely cause is an accidental behavior change in commit timing visible in-process (e.g., a test relying on teardown-commit side effects) — investigate before touching the test, and report every such finding.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/db.py src/marketplace/api.py src/marketplace/auth.py tests/conftest.py tests/test_live_server.py
git commit -m "Commit before the response is sent: CommitRoute on every router (F2)"
```

---

### Task 2: Idempotency middleware — buffer, store, then send (F2b)

**Files:**
- Modify: `src/marketplace/idempotency.py` (`record_send` → buffer; store before send; store-failure tolerance; add module logger)
- Test: `tests/test_live_server.py` (append) and `tests/test_idempotency.py` (append)

**Interfaces:**
- Consumes: Task 1's `live_server` + `slow_commits` fixtures; existing middleware internals (read the whole file first — the replay path, the 409-different-path branch, and the existing `IntegrityError` concurrent-duplicate handling must survive unchanged).
- Produces: response messages buffered and sent only AFTER the `IdempotencyRecord` commit; a store failure logs a warning and still sends the response.

- [ ] **Step 1: Write the failing tests**

tests/test_live_server.py (append):

```python
def test_same_key_immediate_retry_replays_byte_identical(
    live_server: str, basic_service: str, auth: AuthFactory, slow_commits: dict[str, float]
) -> None:
    """F2b: the replay record must be durable before the client can retry.
    slow_commits makes the pre-fix failure deterministic: the record used to
    commit ~150ms after the response, so an immediate same-key retry
    re-executed (observed live: the retry got a 404 where the first call
    made the job)."""
    buyer = auth("buyer", "alice")
    with httpx.Client(base_url=live_server, timeout=10) as c:
        q = c.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
        key = {"Idempotency-Key": "f2b-retry-key"}
        first = c.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer | key)
        second = c.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer | key)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.text == first.text, "same-key retry did not replay byte-identical"
```

tests/test_idempotency.py (append — mirror the file's fixture idiom):

```python
def test_store_failure_never_fails_a_committed_operation(
    client: TestClient, basic_service: str, auth: AuthFactory,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The domain work commits before the record store; a store failure must
    log and still send the success response — a missed replay record beats
    500ing a successful operation."""
    from marketplace import idempotency

    class Boom:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("store exploded (induced)")

    monkeypatch.setattr(idempotency, "IdempotencyRecord", Boom)
    buyer = auth("buyer", "alice")
    q = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    with caplog.at_level(logging.WARNING, logger="marketplace.idempotency"):
        r = client.post(
            "/v1/jobs", json={"quote_id": q.json()["id"]},
            headers=buyer | {"Idempotency-Key": "boom-key"},
        )
    assert r.status_code == 200, r.text  # the job was created and committed
    assert any("idempotency store failed" in rec.getMessage() for rec in caplog.records)
```

- [ ] **Step 2: Run to verify the RIGHT failures**

```bash
uv run pytest tests/test_live_server.py::test_same_key_immediate_retry_replays_byte_identical tests/test_idempotency.py -v
```

Expected: the replay test FAILS deterministically under slow_commits (second response differs — re-execution 404 or a second job); the store-failure test FAILS (today the store isn't wrapped, and the response was already sent — capture the actual pre-fix behavior). Quote red output.

- [ ] **Step 3: Implement — idempotency.py**

Add a module logger (`import logging` + `logger = logging.getLogger("marketplace.idempotency")`, same edit as first use). Replace the forward-as-you-go send with buffering (the replay path and 409-path above it are untouched):

```python
        captured_status = 500
        captured_body = b""
        buffered: list[Message] = []

        async def buffer_send(message: Message) -> None:
            nonlocal captured_status, captured_body
            if message["type"] == "http.response.start":
                captured_status = int(message["status"])
            elif message["type"] == "http.response.body":
                captured_body += bytes(message.get("body", b""))
            buffered.append(message)

        await self.app(scope, receive, buffer_send)

        status = int(captured_status)  # keep the existing pyright-narrowing note
        if status < 500 and status != 401 and status != 403:
            try:
                with SessionLocal() as session:
                    session.add(
                        IdempotencyRecord(
                            principal=principal,
                            key=key,
                            path=path,
                            response_status=status,
                            response_body=captured_body.decode("utf-8", errors="replace"),
                        )
                    )
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()  # concurrent duplicate won the insert; fine
            except Exception:
                # The domain work is already committed (CommitRoute) — a missed
                # replay record beats failing a successful operation.
                logger.warning(
                    "idempotency store failed for %s %s; response sent without replay record",
                    principal,
                    path,
                )
        for message in buffered:
            await send(message)
```

Adapt to the file's actual current code (read it first — variable names, the docstring's send-ordering claims, and the class docstring's ponytail note about the concurrent race, which stays). Update the module docstring: the response is now buffered and sent after the record commit; note the store-failure trade explicitly.

- [ ] **Step 4: Run tests, then everything**

```bash
uv run pytest tests/test_live_server.py tests/test_idempotency.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all green both backends. The oversized-413-not-stored and 401/403-not-stored tests must pass unchanged (the exclusion rules moved position, not semantics).

- [ ] **Step 5: Commit**

```bash
git add src/marketplace/idempotency.py tests/test_live_server.py tests/test_idempotency.py
git commit -m "Store the idempotency replay record before sending the response (F2b)"
```

---

### Task 3: Docs, findings closure, F3 rider, full gates

**Files:**
- Modify: `CLAUDE.md`, `SECURITY.md`, `README.md`, `docs/tryout-findings-2026-07-16.md`

**Interfaces:** consumes Tasks 1-2; produces truthful docs and a closed findings ledger.

- [ ] **Step 1: Docs**

- `CLAUDE.md` — new non-negotiable bullet (place near the concurrency/webhook bullets): commits happen in `db.CommitRoute` BEFORE the response is sent; never re-add a commit to `get_session` teardown (it runs after the response is on the wire — finding F2); every router sets `route_class=CommitRoute` (the import-time `_assert_commit_routes` invariant enforces it); the wrapper commit stays on `run_in_threadpool`; streaming responses would need a rethink of both CommitRoute and the idempotency buffer.
- `SECURITY.md` — (a) update the payments-update residual bullet ("a DB commit failure after a successful provider mutation…"): the client now receives a truthful 500 envelope instead of a false 200, and the provider-ahead-of-DB window self-heals as before; (b) in the observability/idempotency section: the replay record is now durable before the response is sent (sequential same-key retries replay byte-identical); a record-store failure is deliberately non-fatal (logged; retry re-executes against the state machine — the documented concurrent-duplicate posture is unchanged).
- `README.md` — F3 rider: the Disputes line's seller entry becomes `GET /v1/seller/jobs/{id}/dispute` (the `/seller` prefix is currently missing).
- `docs/tryout-findings-2026-07-16.md` — F2 and F2b addendum → **FIXED** (branch, mechanism one-liner, test names); F3 → FIXED (this branch). F4 stays OPEN (maintainer's call, out of scope).

- [ ] **Step 2: Full gates — bare exit codes, both backends, demo**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
uv run python scripts/demo.py
```

Expected: every command exit 0; demo 22 steps (no migration changes on this branch).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md SECURITY.md README.md docs/tryout-findings-2026-07-16.md
git commit -m "Document commit-before-response ordering; close findings F2, F2b, F3"
```

---

## Self-review notes (already applied)

- Spec coverage: §1-§4 (CommitRoute, get_session, failure semantics, routers+invariant) → Task 1; §6 (F2b buffer-store-send + store-failure trade) → Task 2; §7 (fixture + all four test shapes: ordering, read-your-writes, commit-fail, invariant, replay) → Tasks 1-2; §5 (mid-request commits) needs no code — the dispute-pin path is covered by the untouched existing dispute tests, and Task 1 Step 6 explicitly watches for regressions; §8 (docs + F3) → Task 3. Non-goals untouched.
- The deterministic red/green (slow_commits) replaces hope-a-loop-catches-it; the 100-iteration socket loop stays as the field-shaped regression net, with explicit permission for it to pass pre-fix.
- Type consistency: `request.state.db_session` name identical in db.py/tests; `live_server`/`slow_commits` fixture names identical across Tasks 1-2; `_assert_commit_routes` referenced by the invariant unit test.
- Line anchors verified 2026-07-16; match on snippets if drifted.
- lifespan="off" on the fixture is load-bearing (maintenance loop must not mutate the shared test DB) — called out in the fixture docstring itself so a reviewer sees it.
