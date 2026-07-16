# Commit before the response — design (tryout findings F2 + F2b)

**Date:** 2026-07-16
**Status:** Approved approach (maintainer, 2026-07-16: custom APIRoute over
session middleware); spec pending review.

## Problem

**F2:** `db.get_session` commits in FastAPI yield-dependency teardown, and on
this stack (fastapi 0.136 / starlette 1.0 / uvicorn) the commit is not ordered
before the response reaches the client. Proven two ways: live field repro
(fresh quote 404s on immediate use ~10-15% of chained localhost calls, row
visible in Postgres moments later) and a controlled experiment (a 500ms
teardown finishes while the client already holds the response). Consequences:

1. **Read-your-writes race on every write endpoint** — any client chaining
   calls (quote→job, job→cancel, signup→use) can act on a 200 whose
   transaction hasn't committed.
2. **A commit that FAILS in teardown is a silent lie** — the client got a 200
   for work that never persisted, and the error can't reach them.

**F2b (same family):** `IdempotencyMiddleware` streams the response to the
client and only afterwards writes the replay record (own session). An
immediate same-key retry re-executes instead of replaying byte-identical —
observed live (retry got a 404 where the first call created the job; row
locks kept it *safe*, but the replay contract broke).

**Why 270 tests never saw any of this:** `TestClient` awaits the full app
cycle — teardown included — before returning to the test. The bug class is
structurally invisible in-process; only a real network client can observe it.

## Design

### 1. `CommitRoute` — commit ordered by construction

A custom `APIRoute` subclass in **`db.py`** (session lifecycle's home):

```python
class CommitRoute(APIRoute):
    """Commits the request's DB session BEFORE the response object is
    returned to the sender. Never in dependency teardown — teardown runs
    after the response is on the wire (finding F2)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def commit_then_respond(request: Request) -> Response:
            response = await original(request)
            session: Session | None = getattr(request.state, "db_session", None)
            if session is not None and response.status_code < 400:
                await run_in_threadpool(session.commit)
            return response

        return commit_then_respond
```

Correctness is **ordering by construction**: the commit completes before the
route handler hands the `Response` to Starlette's sender — no timing
assumptions.

- **`run_in_threadpool` is mandatory.** The wrapper is async; a bare
  `session.commit()` would put every write's commit on the event loop —
  recreating the async-over-sync problem the webhook offload eliminated.
  This is a binding constraint, not an implementation detail.
- **Exception paths need no commit logic:** an exception inside
  `original(request)` (including `HTTPException`) propagates past the
  wrapper untouched — no commit, teardown rolls back, FastAPI/the
  observability envelope shape the response exactly as today.
- **Status guard `< 400`:** a handler that *returns* an error-shaped response
  (rather than raising) must not commit.

### 2. `get_session` — stash, stop committing

```python
def get_session(request: Request) -> Iterator[Session]:
    session = SessionLocal()
    request.state.db_session = session
    try:
        yield session
    finally:
        session.close()  # close() discards (rolls back) anything uncommitted
```

- The injected `Request` is how the session reaches `CommitRoute` — no
  contextvars, no globals. FastAPI's per-request dependency cache guarantees
  one session per request (api.py's `SessionDep` and auth.py's `_SessionDep`
  both resolve the same cached `get_session`), so the stash is single-valued.
- Teardown becomes close-only. On the success path the wrapper already
  committed; on any exception path `close()` rolls back — preserving today's
  "a 4xx/5xx never persists partial work" guarantee.
- No callers break: `get_session` is consumed exclusively via `Depends`
  (verified by grep) — nothing calls it directly.

### 3. Failure semantics become a feature

A commit that fails in the wrapper **raises before the response exists** →
propagates to `RequestIdMiddleware` → clean
`500 {"detail": "internal error", "request_id": …}` and the teardown rolls
back. Today the same failure yields a lying 200. This is a tested behavior:
induced commit failure → 500 envelope, nothing persisted.

### 4. Rollout: 7 routers + a startup invariant

`route_class=CommitRoute` on every router:

- api.py: `reports_router`, `prefs_router`, `buyer_router`, `seller_router`,
  `admin_router`, `payments_router`
- **auth.py: `auth_router`** (line ~154 — easy to miss; it's the one router
  defined outside api.py)

**Startup invariant** (module level in api.py, right after the
`include_router` block, so it fails at *import* — tests never run the
lifespan): every `APIRoute` whose path starts with `/v1` must be a
`CommitRoute`, else raise. Forgetting `route_class=` on a future router
becomes an immediate boot/collection failure instead of a silent F2
regression. `/healthz` (the only non-`/v1` route, sessionless) is exempt.

### 5. Handler-owned mid-request commits stay legal and unchanged

`resolve_dispute` deliberately commits mid-handler (pinning
`refund_amount`/`clawback_amount` before the provider legs so a 502-retry
converges on the same amounts). Under this design: the pin commit is
untouched; a post-pin exception still rolls back only post-pin work (close()
discards it; the pin is already committed); on success the wrapper commits
the post-pin work. Identical semantics — the wrapper commit is purely
additive at the end. The stale-relock lesson attached to mid-request commits
is unaffected.

The webhook path also needs nothing: `_process_webhook` already commits its
own session inside `asyncio.to_thread` *before* the handler returns — it was
accidentally the only write path that was ever correctly ordered.

### 6. F2b — idempotency middleware: buffer → store → send

`IdempotencyMiddleware.record_send` currently forwards messages as they
arrive and stores afterwards. Change: **buffer** the response messages
(don't forward), write the `IdempotencyRecord` (same store-exclusion rules
as today: never 5xx/401/403), **then** replay the buffered messages to
`send`. Ordering becomes: domain commit (CommitRoute, inside the wrapped
app) → replay record stored → client sees the response.

- **A store failure must not fail a succeeded operation:** the domain work is
  already committed by the time the store runs, so wrap the store in
  try/except — log a warning and send the response anyway. A missed replay
  record (retry re-executes, state-machine 409s apply, as today) is strictly
  better than 500ing a successful charge.
- Buffering is bounded: responses are small JSON and
  `BodySizeLimitMiddleware` caps requests at 1 MiB upstream; the app has no
  streaming responses (constraint noted below).
- The **concurrent**-duplicate race (two same-key requests in flight at once)
  stays out of scope — documented ponytail in idempotency.py, unchanged; the
  DB row locks keep it safe.

### 7. The real-uvicorn test fixture (the heart of the branch)

New fixture (session-scoped, `tests/test_live_server.py` or conftest): start
`uvicorn.Server` for `api.app` in a thread on an ephemeral port against the
suite's existing temp SQLite file DB (uvicorn is already a runtime
dependency; the race is process-ordering, not DB behavior, so SQLite keeps it
in the default suite); poll `/healthz` until up; `should_exit=True` at
teardown. Tests drive it with httpx over the real socket:

- **Read-your-writes:** tight loop (≥30 iterations) of quote→immediate-job
  via the socket — zero "quote not found". This is the exact live repro shape
  (failed 2/30 pre-fix).
- **F2b sequential replay:** POST `/v1/jobs` with an `Idempotency-Key`, then
  immediately re-POST the same key — byte-identical body both times,
  repeated ≥10x.
- **Commit-failure → 500 envelope:** in-process is fine for this one
  (ordering isn't what's tested, failure semantics are). Mechanism:
  `app.dependency_overrides[get_session]` with a wrapper that yields the real
  session wrapped so its first `commit()` raises `OperationalError`; assert
  500 envelope shape, `X-Request-ID` present, and nothing persisted; clear
  the override in a finally.
- **Startup invariant:** unit-test the checker against a scratch FastAPI app
  containing one rogue non-CommitRoute `/v1` route — expect it to raise.

### 8. Docs (+ the F3 rider)

- `CLAUDE.md` non-negotiables: commits happen in `CommitRoute` before the
  response is sent — never re-add a commit to `get_session` teardown; every
  new router must set `route_class=CommitRoute` (the import-time invariant
  enforces it); the wrapper commit stays on `run_in_threadpool`.
- `SECURITY.md`: update the payments-update residual ("a DB commit failure
  after a successful provider mutation…") — the client now receives a 500,
  not a false 200; note the idempotency store ordering change and the
  explicitly-accepted store-failure → missed-replay trade.
- `docs/tryout-findings-2026-07-16.md`: F2/F2b → FIXED.
- **F3 rider:** README's dispute line gains the `/seller` prefix
  (`GET /v1/seller/jobs/{id}/dispute`) — one line.

## Testing summary

Real-socket: read-your-writes loop, F2b replay loop. In-process: commit-fail
→ 500-envelope + nothing-persisted, invariant checker, store-failure →
response-still-sent (idempotency), full both-backend suites, demo, and the
existing PG race tests (unchanged — they guard locking, which this branch
must not touch).

## Non-goals

- Async SQLAlchemy / async sessions — a rewrite the template deliberately
  avoids; `run_in_threadpool` keeps sync sessions correct.
- The concurrent-duplicate idempotency race (documented, safe, out of scope).
- F4 (200-vs-201 consistency) — public API shape change, maintainer's call,
  separate.
- Streaming responses — none exist; `CommitRoute` and the F2b buffer assume
  small, fully-materialized JSON responses. A fork adding streaming revisits
  both (noted in CLAUDE.md).
- Background tasks — none are used; if a fork adds them, they run after the
  response and must open their own sessions (same rule as the maintenance
  loop).
