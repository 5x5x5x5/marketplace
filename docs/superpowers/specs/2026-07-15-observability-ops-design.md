# Observability & ops — design

**Date:** 2026-07-15
**Status:** Approved (maintainer, 2026-07-15: admin stats endpoint; JSON logs w/ plain
toggle; 7/30/30-day retention, env-tunable; full ops bucket in one branch)

## Problem

The template is feature-complete for a pilot but not yet runnable-by-one-person:

1. **No request logging or correlation** — loggers exist (`marketplace`,
   `.auth`, `.notifications`, `.mail`) but nothing configures them, there is no
   access log, and no request ID ties an error to a request.
2. **No error envelope** — an unhandled exception surfaces Starlette's default
   500; nothing guarantees internals never leak, and there's no request-id to
   report.
3. **No operator dashboard** — the only aggregate view is the margins summary.
4. **Unbounded tables** — `idempotency_keys`, `webhook_events`, and
   SENT/FAILED `notifications` rows grow forever.
5. **Webhook handler blocks the event loop** — `payments_webhook` is
   `async def` doing sync `Session` work inline.
6. **API-hardening gaps** — no TrustedHost/CORS/body-size limits; three admin
   lists unpaginated (`/v1/admin/reviews/{kind}`, `/v1/admin/reports`,
   `/v1/admin/buyers`); `seller_profiles.provider_account_id` and
   `payouts.provider_transfer_id` unindexed; the promised PG cancel-vs-webhook
   race test never landed.

This is the LAST template feature (maintainer decision): after this, RBAC / OAuth /
gateway rate-limiting are fork work.

## Design

### 1. Logging config + request-ID middleware

**`src/marketplace/observability.py`** (new module) owns all of it:

- `request_id_var: contextvars.ContextVar[str]` — default `"-"`.
- `JsonFormatter(logging.Formatter)` — stdlib-only (~20 lines): emits one JSON
  object per line with `ts` (ISO-8601 UTC), `level`, `logger`, `msg`
  (`record.getMessage()`), `request_id` (from the contextvar), plus
  `exc_info` rendered into an `exc` string when present. No new dependency.
- `RequestIdFilter(logging.Filter)` — stamps `record.request_id` so the plain
  format can show it too.
- `configure_logging()` — `logging.config.dictConfig`: root at `INFO` to
  stderr; formatter chosen by `settings.log_format` (`"json"` default,
  `"plain"` → `"%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"`).
  Folds `uvicorn`, `uvicorn.error`, `uvicorn.access` into the same handlers
  (uvicorn's own access line is disabled — ours replaces it). Idempotent
  (safe to call twice; tests call it directly).
- `RequestIdMiddleware` — pure ASGI, same style as `IdempotencyMiddleware`,
  mounted OUTSIDE it (added last): honors inbound `X-Request-ID` (truncated to
  64 chars, printable ASCII only — otherwise regenerate), else
  `uuid4().hex`; sets the contextvar for the request's lifetime; adds
  `X-Request-ID` to the response headers; after the response completes, logs
  one line to the `marketplace.access` logger:
  `method path status duration_ms` (as structured extra fields in JSON mode).
  `/healthz` is skipped (poll noise).
- `configure_logging()` is called at import-time of `api.py` app assembly
  (before the lifespan), so CLI (`uvicorn marketplace.api:app`), tests, and
  the demo all get it.

Access-log lines never include headers, bodies, or query strings (tokens ride
in headers; reset tokens ride in bodies) — method, path, status, duration,
request-id only. This is a security boundary, not a style choice.

### 2. Error envelope — lives INSIDE RequestIdMiddleware

A separate `@app.exception_handler(Exception)` would run in Starlette's
`ServerErrorMiddleware`, which sits OUTSIDE user middleware — the request-id
header stamp and contextvar lifetime both break on that path. So the envelope
is the middleware's own `except Exception` arm (one cohesive observability
middleware: request-id + access log + envelope):

- catch any exception that escapes the inner app (FastAPI's
  `ExceptionMiddleware` is inside us, so `HTTPException` and 422 validation
  responses never reach this arm),
- `logger.exception(...)` the traceback (request-id correlated, ERROR level),
- log the access line with status 500,
- send `500 {"detail": "internal error", "request_id": <id>}` with the
  `X-Request-ID` header, and do NOT re-raise.

The traceback goes to the log; the response body carries nothing else. Because
the middleware handles the exception itself, `TestClient` needs no
`raise_server_exceptions` gymnastics in tests.

### 3. `GET /v1/admin/stats`

Admin-only, one session, `count()` queries only — a curl-able operator
snapshot, `StatsOut` model:

- `jobs: dict[str, int]` — count by `JobStatus`
- `payments: dict[str, int]`, `payouts: dict[str, int]` — by status
- `notifications: {pending, sent, failed, oldest_pending_age_seconds | null}`
- `disputes_open: int` (status OPEN), `reports_open: int` (status OPEN)
- `users: dict[str, int]` — by role, plus `"suspended"` count
- `quotes_live: int` (unexpired)
- `retention: {idempotency_keys, webhook_events, notifications_total}` — row
  counts of the swept tables, so retention is observable
- `uptime_seconds: int` — process start stamped at module import

Missing enum values report 0 (build the dict from the enum, not the query).

### 4. Retention sweeps

`_sweep_retention(session)` in `api.py` joins `_sweep(...)` (runs on the
maintenance-loop clock and the lazy paths):

- `IdempotencyRecord.created_at < now - retention_idempotency_days` → delete
- `WebhookEvent.received_at < now - retention_webhooks_days` → delete
- `Notification.created_at < now - retention_notifications_days` AND
  `status IN (SENT, FAILED)` → delete. **PENDING rows are never deleted at any
  age** — the outbox contract holds.

Settings (env-tunable): `retention_idempotency_days: int = 7`,
`retention_webhooks_days: int = 30`, `retention_notifications_days: int = 30`.
Deletes are bulk `session.execute(delete(...))`, same idiom as
`_sweep_expired_auth`.

Retention consequence, documented in SECURITY.md: a provider webhook replayed
AFTER its dedup row aged out (>30d) would re-apply. Every `_apply_payment_event`
transition is already guarded/terminal (REFUNDED terminal, SUCCEEDED never
downgraded), so a stale replay no-ops at the state machine instead — note it,
don't code for it.

### 5. Webhook DB work off the event loop

`payments_webhook` keeps `async def` (it must `await request.body()`), keeps
signature verification inline (cheap HMAC), then moves ALL DB work to a worker
thread: drop the `SessionDep`/`ProviderDep` request-scoped session, extract the
dedup + `WebhookEvent` insert + `_apply_payment_event` + commit into a sync
`_process_webhook(event) -> dict[str, str]` that opens its own
`SessionLocal()` (the `_run_drain_once` precedent), and call it via
`await asyncio.to_thread(_process_webhook, event)`. Wire-visible behavior is
identical: same responses, same dedup, same 400s (signature/malformed checks
happen before the thread hop).

### 6. API hardening

- `TrustedHostMiddleware(allowed_hosts=settings.trusted_hosts)` —
  `trusted_hosts: list[str] = ["*"]` default (off until configured).
- `CORSMiddleware` added ONLY when `settings.cors_origins` is non-empty
  (`cors_origins: list[str] = []`), `allow_credentials=True`, all methods/headers.
- `BodySizeLimitMiddleware` — pure ASGI: reject `Content-Length >
  settings.max_body_bytes` (default 1 MiB) with 413 immediately, and cap
  chunked/streamed bodies by counting received bytes (413 mid-stream).
  Mounted outside IdempotencyMiddleware so oversized bodies are never stored.
- Paginate the three unpaginated admin lists with the existing
  `Limit`/`Offset`/`_paginate` idiom (default limit 100, same as siblings).
- **Migration #10:** `op.create_index` on `seller_profiles.provider_account_id`
  and `payouts.provider_transfer_id` (both looked up by webhook handlers).
  Entity columns gain `index=True` to match.

Middleware order (outermost first): RequestId → BodySizeLimit → TrustedHost →
CORS (conditional) → Idempotency → app. (In `add_middleware` terms: Idempotency
added first, RequestId added last.)

### 7. Payments-hardening tests + audit

- PG-gated cancel-vs-webhook race test: admin cancel of an AWAITING_PAYMENT
  job racing a concurrent `payment_succeeded` webhook — exactly one side wins;
  the loser 409s/no-ops; never a voided-and-accepted job, never a paid job
  expired. (True parallel writes only real on Postgres — same gating idiom as
  the existing concurrency tests.)
- Secret-echo audit: SECURITY.md gets the standing rule spelled out (no
  endpoint may return a secret in a response stored for idempotent replay;
  `/v1/auth/*` is excluded from the store — already regression-tested; the
  PaymentIntent `client_secret` in the accept response is buyer-facing BY
  DESIGN and scoped per-principal in the store). Final-review instruction:
  audit any new secret-returning POST against this rule.

### 8. Demo + docs

- Demo act 8: hit `/v1/admin/stats`, assert jobs/payments counts are coherent
  with the run so far and `X-Request-ID` is present on the response; send an
  inbound `X-Request-ID: demo-run-123` and assert it round-trips.
- `ROADMAP.md`: observability item → done; remaining ahead-list shrinks to
  RBAC / API-gateway extras / OAuth (fork work note per maintainer decision).
- `README.md`: stats endpoint, request-id behavior, log format toggle, new
  env vars table entries.
- `SECURITY.md`: envelope guarantee, access-log redaction stance, retention
  windows + stale-replay note, body-size cap, TrustedHost/CORS defaults.
- `CLAUDE.md`: migration total 10; non-negotiables: access logs never carry
  headers/bodies/query strings; PENDING outbox rows are never reaped;
  webhook DB work stays off the event loop.

## Testing

- Request-id: generated when absent; inbound honored (and sanitized: >64 chars
  or non-printable → regenerated); echoed on responses including 404s/500s;
  contextvar visible in app-logger lines during a request.
- JSON log shape: capture a line via caplog/capsys, `json.loads` it, assert
  keys (`ts`, `level`, `logger`, `msg`, `request_id`); plain toggle smoke test.
- Envelope: a test-only route (registered in the test, not shipped) raises
  `RuntimeError` → 500, body exactly `{"detail": "internal error",
  "request_id": <echoed>}`, `X-Request-ID` header present, no traceback text
  in body, traceback IS in the log.
- Stats: seed known rows → exact counts; empty DB → all-zero dicts with full
  enum keys; suspension/role breakdown correct.
- Retention: rows at boundary-minus-a-day stay, boundary-plus-a-day go (freeze
  timestamps by writing rows directly); PENDING notification older than the
  window survives; sweep is idempotent.
- Body cap: Content-Length over limit → 413 (and not stored by idempotency);
  under limit passes.
- TrustedHost: bad Host → 400 when configured; default `*` accepts.
- Webhook offload: existing webhook suite stays green (behavior-preserving);
  one test asserts the response still round-trips under TestClient.
- PG race test as §7.
- Both backends; fresh-volume migration chain = exactly 10.

## Non-goals

- Prometheus/OpenTelemetry exporters, log shipping, alerting, tracing spans —
  fork layers them on (the stats endpoint and JSON logs are the hooks).
- Per-endpoint latency histograms/metrics counters beyond the stats snapshot.
- Gateway rate-limiting, admin RBAC, OAuth (fork work — maintainer decision).
- Log files/rotation (stderr only; the process manager owns files).
- Backfilling request-ids into audit/idempotency tables.
