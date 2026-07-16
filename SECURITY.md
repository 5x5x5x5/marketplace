# Security posture

A full read-only sweep was done on the v1 scaffold; the **safe-to-pilot
hardening** closed the exploitable findings (status table below). Eight
updates followed, in order: the **template build** (moved state to Postgres),
**payments** (added an escrow provider), **real-user auth** (replaced the
pilot HMAC tokens with DB-backed sessions), **disputes** (added arbitration
over the escrow — partial refunds/clawbacks and chargeback recording),
**moderation** (suspension, comment takedown, and counterparty abuse
reports), **notification preferences** (per-kind mutes with a
server-side money floor), **fee-aware margin** (admin-tunable
provider-fee estimate stamped onto every charge, floor enforced net of it),
and **observability & ops** (request-id tracing, a global 500 envelope, an
admin stats endpoint, retention sweeps, webhook DB work off the event loop,
and API hardening — body cap, TrustedHost, CORS) — see the update notes
below.

## Update — template build

- **Concurrency is now the database's job.** The process-wide lock is gone;
  quote consumption, job/offer status transitions, and seller-capacity checks use
  `SELECT … FOR UPDATE` row locks. On Postgres these are real; the SQLite test
  backend serializes writes, so the deterministic guard tests hold on both, and a
  true-parallel test can run against Postgres via `DATABASE_URL`.
- **Tokens now expire** (`exp` claim; `TOKEN_TTL_HOURS`), closing the
  never-expiring-token gap. Still pilot-grade HMAC — not production auth.
  *(Superseded — see "Update — real-user auth" below: HMAC tokens and
  `TOKEN_TTL_HOURS` are deleted, not just expired.)*
- **Seller capacity** is enforced under a row lock on accept, so a seller can't
  exceed their configured concurrent-job limit even under racing accepts.
- **Money is `Decimal`** end-to-end; the margin floor is enforced on quantized
  values, removing the float sub-floor-leakage drift (old M3).
- **Residual (Low, mitigated):** a hand-crafted non-compliant JSON body
  (`NaN`/`Infinity`) is rejected (never stored) but still surfaces as a 500
  rather than a clean 422. The global error envelope shipped (see "Update —
  observability & ops" below) means that 500 is now a clean
  `{"detail": "internal error", "request_id"}` body with the crash confined
  to the log, not a raw traceback — see finding **N1**.

## Update — payments

- **The webhook endpoint (`POST /v1/payments/webhook`) is unauthenticated but
  signature-verified.** There's no bearer token to check — the provider can't
  present one — so authenticity comes from `parse_webhook` validating the
  provider's signature header before anything is applied; an invalid signature
  is a 400. Every event is recorded in `WebhookEvent` keyed on the provider's
  event id before it's applied, so a replayed delivery (the provider retries on
  a slow 2xx, or an attacker replays a captured payload) is a no-op, not a
  double-apply.
- **`client_secret` is exposed only to the owning buyer, and only while a
  payment is awaited.** The secret is populated exclusively by `_buyer_view`
  (`api.py`), for the owning buyer, while the job is `AWAITING_PAYMENT`; it's
  `None` once the charge succeeds. The admin job routes reuse the `BuyerJobView`
  schema but bypass `_buyer_view` enrichment, so `payment_status`/`client_secret`
  are always `None` there, and no seller view carries the field at all.
- **Refunds/voids are admin- or owner-initiated only.** Job cancellation runs
  through the same buyer/admin-owned cancel path as before; there's no
  standalone refund endpoint a caller could hit directly.
- **Client idempotency responses are stored per-principal.** `IdempotencyRecord`
  is keyed on `(principal, key)` (`idempotency.py`), so one caller can never
  replay another's cached response even if the header value collided.
- **Residual:** the fake payment provider accepts unsigned webhook JSON by
  design (`payments/fake.py` — there's no real provider to sign anything in
  dev/tests) and must never be reachable in production. Selection is
  env-driven (`STRIPE_SECRET_KEY`) with no runtime override, so this is a
  deployment-configuration risk, not a code path an attacker can toggle.
- **Residual:** a DB commit failure after a successful provider mutation leaves
  the provider briefly ahead of the database — charge and refund self-heal on
  retry via idempotency-key replay, void self-heals via the idempotent cancel
  (already-canceled counts as success), and a transactional outbox is the
  eventual upgrade path.

## Update — real-user auth

Pilot-grade HMAC tokens (`mint_token`, `MARKETPLACE_SECRET`) are gone —
deleted, not deprecated. Identity now resolves through DB-backed sessions:

- **Sessions are opaque bearers, hashed at rest.** `POST /v1/auth/signup` and
  `POST /v1/auth/login` issue a random 32-byte token; only its sha256 is
  stored (`auth_sessions.token_hash`). A leaked database dump doesn't hand out
  usable bearer tokens. Every authenticated request does one indexed lookup
  (`token_hash` + `expires_at > now`) to resolve `(role, user_id)`.
- **Sessions are revocable, not just expiring.** Logout deletes the one row
  for that token. A password reset deletes *every* session row for that user
  (`confirm_password_reset`) — a stolen token stops working the moment the
  owner resets their password. The same row-deletion path is where a future
  ban/suspend would hook in. Sessions also carry a TTL
  (`SESSION_TTL_HOURS`, default 72); expired rows are left for the lazy sweep
  rather than actively purged.
- **Passwords are argon2id via `pwdlib`** (`auth.hash_password`/
  `verify_password`), never stored or logged in plaintext.
- **Login gives no account-existence signal.** An unknown email still runs a
  password verify against a fixed dummy hash before returning the same 401 as
  a wrong password, so response timing doesn't distinguish "no such account"
  from "wrong password."
- **Password-reset-request is uniform-200 by design** (`POST
  /v1/auth/password-reset/request` always replies `{"status": "ok"}`,
  independent of whether the email/role exists) — no enumeration via status
  code or body.
- **One account is one email + one role.** The same email may hold a separate
  buyer account and seller account; `(email, role)` is the uniqueness key, not
  `email` alone. There's no self-serve admin signup — the admin account is
  seeded once at startup from `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- **Email verification and password reset ride the same `EmailSender` port**
  (`src/marketplace/mail.py`). The shipped `ConsoleEmailSender` logs instead of
  sending. Mail-send failure is a guarded boundary: `_issue_email_token`
  catches and logs, so a broken/slow mail provider never fails or
  fingerprints the enclosing request (signup still 201s; reset-request still
  200s) — the token row is flushed-pending on the request's session and
  commits at request end; swallowing the send failure is what lets that
  commit proceed, so the user can re-request.

**Residuals, named rather than hidden:**
- **No login rate-limiting.** Brute-forcing a password is not throttled at
  the app layer; this is deliberately deferred to the gateway (see
  `ROADMAP.md`'s API-hardening item), same posture as the rest of the API.
- **A timing delta remains on `password-reset/request`.** The response body
  and status code are identical either way, but the exists-branch does
  strictly more work (an `EmailToken` insert plus a send attempt) than the
  ghost branch, so wall-clock time leaks a faint signal. Closing it needs
  constant-time work on the ghost path, which isn't built. Accepted at pilot
  grade alongside the no-rate-limiting posture above.
- **Email verification gates nothing yet.** `POST /v1/auth/verify` flips
  `email_verified`, but no endpoint checks that flag — signup and login both
  work on an unverified account, because the console adapter can't prove
  mail was actually deliverable. A fork wiring in a real sender is expected
  to add the gate at the same time.

## Update — disputes

- **Opening a dispute is buyer-only, with ownership, window, and one-per-job
  guards.** `POST /v1/jobs/{id}/dispute` requires the caller's session to
  resolve to the job's own buyer (404 otherwise, same not-yours pattern as
  job views); the job must be `COMPLETED` and within `DISPUTE_WINDOW_DAYS` of
  `completed_at` (409 once the window elapses); a job can carry at most one
  dispute row, ever (409 on a second attempt). A Stripe chargeback on the
  same job annotates that existing row instead of creating a duplicate.
- **Resolving a dispute is admin-only, with bounds validated at the trust
  boundary.** `POST /v1/admin/disputes/{id}/resolve` requires an admin
  session; `refund_amount`/`clawback_amount` are quantized and range-checked
  (0..`buyer_price`, 0..`seller_payout`) before either provider call, so a
  hand-crafted body can never authorize refunding or clawing back more than
  the job itself moved. The two provider legs (partial refund, partial
  transfer reversal) are each idempotent by a dispute-scoped key
  (`refund:{job_id}:dispute`, `reversal:{job_id}:dispute` — deliberately
  distinct from the cancel path's keys); a failure on either leg records
  nothing, and a retry replays the succeeded leg instead of double-moving
  money. `Payment.status` is never touched by a partial refund — the charge
  stays `SUCCEEDED`, so a partial refund can never be mistaken for (or
  collide with) the cancel path's full `REFUNDED` state.
- **The chargeback webhook branch never 500s on an unknown or replayed
  event.** `charge.dispute.created`/`closed` locate the `Payment` by
  `related_id`; an id that doesn't map to a known payment is recorded (dedup
  ledger) and ignored, not a crash. The branch rides the existing
  signature-verified, `WebhookEvent`-deduped `/v1/payments/webhook`, so a
  replayed chargeback event no-ops like every other event kind. An admin's
  arbitration outcome (`resolved`) is preserved even if a chargeback closes
  on the same job afterward — the loss/fee still lands in the `adjustments`
  ledger, but the `status` field keeps recording the arbitration outcome
  rather than being overwritten; a still-`open` dispute lets the latest
  provider outcome set the status instead.
- **Dispute views stay role-scoped like job views.** `BuyerDisputeOut` never
  carries the clawback amount; `SellerDisputeOut` never carries the refund
  amount; only `AdminDisputeOut` carries both. `reason`/`status` are visible
  to all three (the seller has to know what they're accused of).
- **Seller→buyer reviews expose only the aggregate to the buyer.**
  `GET /v1/profile` returns `rating`/`rating_count`, never the individual
  `SellerReview` rows or comments; those stay admin-side (`GET
  /v1/admin/buyers` is the same aggregate, not a review list). The
  moderation sub-phase revisited this: each party can now read their own
  job's review row(s) — no party ids, hidden comments read null (see the
  moderation update below); cross-job review lists stay admin-side.

## Update — moderation

- **Reporting is counterparty-gated, not open season.** `POST /v1/reports`
  only accepts a `user` target who shares a job with the reporter (either
  direction) or a `review`/`seller_review` target the reporter is a party
  to (author or subject) — a 403 otherwise, and a self-report is a 422. One
  report per `(reporter, target)` pair, enforced by a DB `UNIQUE` so a
  sequential or concurrent duplicate both land the same 409, never a 500.
  Every filing notifies every admin (`REPORT_OPENED_ADMIN`) inside the same
  transaction as the insert, via the existing enqueue-only outbox — no
  filing is silently unseen.
- **The reporter never sees the resolution note.** `ReportOut` (the
  reporter's own `GET /v1/reports` view) carries `status` but not
  `resolution_note`; that field exists only on `AdminReportOut`. The
  admin's internal reasoning for actioning or dismissing a report is never
  echoed back to the person who filed it.
- **Resolving a report is terminal and inert.** `POST
  /v1/admin/reports/{id}/resolve` requires `status` to already be
  `open` (409 on a second resolve — no re-resolution, no status flip-flop)
  and only writes `status`/`resolution_note`/`resolved_at` onto the report
  row itself; it never touches the reported user, review, or job. Any
  consequence (suspend, hide) is a deliberate, separate admin action —
  resolving a report is not an automatic trigger.
- **Suspension is verb-gated, not a global lock, and admins are exempt by
  construction.** `suspend_user` 422s on a target whose `role is ADMIN`
  before anything is written, so an admin account can never be suspended
  (self or otherwise). For everyone else, `_require_active` gates only the
  acquisition verbs (quotes, jobs, reviews, disputes, availability,
  offer-accept, payments-onboard, report-filing) with a `403 {"detail":
  "account suspended"}` — the only place suspension surfaces to the
  suspended user; login, `GET`s, `complete`, `decline`, and `cancel` stay
  reachable so in-flight work finishes. `repo`'s seller-matching query
  anti-joins `UserStatus.SUSPENDED`, so a suspended seller stops receiving
  new offers without a matching-side special case. Suspension itself never
  calls a payment provider or touches a `Transaction`/`Payment` row — freeze
  new, finish in-flight, move no money.
- **Takedown hides, it never deletes.** `comment_hidden` is a boolean on
  `Review`/`SellerReview`; hiding clears the comment from every non-admin
  view (the reviewee's own reads, `GET /v1/profile`, etc. — `comment_hidden`
  is the one property those views gate on) while `rating` and every rating
  aggregate stay untouched — a hidden review still counts toward
  `rating`/`rating_count`, only the free-text vanishes. Admin's
  `GET /v1/admin/reviews/{kind}` and the hide/unhide endpoints always
  return the raw `comment` plus the `comment_hidden` flag — admins can
  always see what was hidden and why it was actioned. `POST
  /v1/admin/users/{id}/reset_display_name` is idempotent-safe and
  deterministic (`user-{id[:8]}`), independent of the takedown path so it
  can be applied to a harassment-via-display-name case with no review
  involved.
- **Job parties can read their own job's reviews.** `GET /v1/jobs/{id}/reviews`
  (buyer) and `GET /v1/seller/jobs/{id}/reviews` (seller) return that job's
  review(s) (id, kind, rating, comment, created_at) — no party ids, and a
  hidden comment reads as `null` even to the job's own parties — giving the
  subject of an abusive review the id it needs to file a report against it.

## Update — notification preferences

- **Mutes are per-kind, with a server-side money floor that no path can
  bypass.** `refund_issued_buyer`, `dispute_resolved_buyer`,
  `dispute_resolved_seller`, and `payout_failed_admin` can never be muted —
  not via `PUT /v1/notification-preferences` (422), and not via a direct
  `NotificationMute` row inserted straight into the database, because the
  floor is enforced at `enqueue` (`notifications.MUST_SEND`), not at the API
  boundary. The outbox is what will actually send, so that is where the
  floor has to hold.

## Update — fee-aware margin

- **`Payment.fee_estimate` is an estimate, not reconciled provider actuals.**
  It's computed from the admin-tunable `pct`/`fixed` platform config (`PUT
  /v1/admin/config/fees`, defaulting to Stripe's 2.9% + 30¢) and stamped onto
  the row once, at charge time — it is never recomputed later, so a
  subsequent config change doesn't retroactively reprice historical charges,
  and it can drift from what the provider actually deducts (a true
  reconciliation would read the provider's charge/balance-transaction fee,
  which is fork work). Pre-migration payment rows (charged before migration
  #9) carry `0`, not a backfilled estimate.
- **The margin floor is enforced net of the fee estimate, at quote/match
  time, so a floor-priced job can't be signed at a loss — for the fee config
  in force at quote/match time; a fee-config change between quote and accept
  is applied at the stamp, the same eventual-consistency stance as the margin
  floor itself.** Both enforcement sites — the quote-path check and
  match-time candidate filtering (`passes_floor`) — compare against
  `matching.required_spread(buyer_price, margin_floor, fees)`, never the
  gross spread; see the "Money is `Decimal`" bullet above.

## Update — observability & ops

- **The envelope guarantee: unhandled errors never leak internals.** A
  single request-boundary middleware (`RequestIdMiddleware`) wraps every
  route; any exception that escapes an endpoint becomes a clean
  `{"detail": "internal error", "request_id": "<id>"}` 500 with no
  traceback, no exception message, and no stack frame in the response
  body — the traceback goes to the application log only, keyed by the
  same request id. This is deliberately middleware, not
  `@app.exception_handler` — a FastAPI/Starlette exception handler runs
  *outside* user middleware (inside `ServerErrorMiddleware`), which would
  lose both the `X-Request-ID` response header and the request-id
  contextvar on exactly the requests where they matter most. `404`/`422`s
  keep their normal FastAPI shapes; only truly unhandled exceptions are
  enveloped (`tests/test_observability.py::test_envelope_hides_internals`,
  `::test_http_exceptions_not_enveloped`). This also closes finding **N1**
  below: a hand-crafted non-compliant JSON body (`NaN`/`Infinity`) that
  crashes error serialization no longer leaks a raw traceback — it still
  surfaces as a 500 (not a clean 422; a stricter transport-level JSON
  parser would be needed for that), but now as the same clean envelope
  as every other unhandled error, with the crash detail confined to the
  log.
- **Access logs never carry headers, bodies, or query strings — only
  method, path, status, duration, and request id.** Bearer tokens ride in
  the `Authorization` header, and reset/verification tokens ride in
  request bodies or (if a fork ever GETs them) query strings; logging any
  of the three would put a live credential in a log aggregator most
  incident responders don't treat as a secrets store. `path` is logged
  without its query component for the same reason
  (`tests/test_observability.py::test_access_log_line_shape_and_redaction`
  asserts a `?secret=…` query string never reaches the log line).
  `GET /healthz` is excluded from the access log entirely (liveness-probe
  noise).
- **Retention sweeps keep three tables bounded; the outbox itself is
  exempt.** `idempotency_keys` (`RETENTION_IDEMPOTENCY_DAYS`, default 7),
  `webhook_events` (`RETENTION_WEBHOOKS_DAYS`, default 30), and terminal
  SENT/FAILED `notifications` (`RETENTION_NOTIFICATIONS_DAYS`, default
  30) age out on the maintenance loop's clock. PENDING `notifications`
  rows are never reaped regardless of age — sweeping a row that still
  needs to send would silently drop a buyer/seller/admin notification,
  which is worse than an unbounded table. **Stale-webhook-replay note:**
  once a `webhook_events` row ages past the retention window, a
  redelivered event for it no longer dedups against that row — but
  `_apply_payment_event` re-applying the same event is still safe,
  because the state transitions it drives are themselves terminal-guarded
  independent of the dedup table: a `payment_succeeded` replay against an
  already-`REFUNDED` or non-`AWAITING_PAYMENT` job no-ops
  (`api.py::_apply_payment_event`, "refunded is terminal" /
  "late success must never resurrect"), and a `payment_failed` replay
  never downgrades an already-`SUCCEEDED` payment. The dedup table is a
  fast-path optimization against duplicate provider retries, not the only
  thing standing between a replay and a double-apply. The analogous gap
  exists on the client side: an `Idempotency-Key` replayed after its own
  `RETENTION_IDEMPOTENCY_DAYS` window re-executes the request rather than
  replaying the cached response — but the money paths stay safe regardless,
  the same way: the offer/job state machine 409s a repeat accept/decline
  against an already-resolved offer, a consumed quote 404s or 410s on a
  second `POST /jobs`, and the charge carries its own provider-side
  idempotency key (`charge:{job_id}`, independent of the client's header)
  as an extra short-horizon layer — providers retain such keys briefly
  (Stripe: ~24h), so it's the state-machine guards, not the provider key,
  that hold at any age.
- **Body cap default is 1 MiB (`MAX_BODY_BYTES=1_048_576`).** Checked
  against a declared `Content-Length` up front (fast rejection, no body
  read) and independently counted on chunked/streamed bodies so a caller
  can't dodge the cap by omitting `Content-Length`; either path returns a
  413 before the oversized body reaches an endpoint or the idempotency
  store (`BodySizeLimitMiddleware` mounts outside `IdempotencyMiddleware`
  so a 413 is never itself cached for replay).
- **`TrustedHostMiddleware`/`CORSMiddleware` ship open by default, narrow
  in production.** `TRUSTED_HOSTS=["*"]` and `CORS_ORIGINS=[]` (no CORS
  headers at all) are the out-of-the-box dev posture — a fork deploying
  behind a real domain sets both explicitly (`TRUSTED_HOSTS` to its
  hostname(s), `CORS_ORIGINS` to its frontend origin(s)) before going to
  production; neither is enforced by this template, by design (it's a
  generic starting point, not a specific deployment).
- **The idempotency secret-echo standing rule holds, and is tested.**
  `IdempotencyMiddleware` never stores a `401`/`403` response for replay
  (`idempotency.py`; `tests/test_idempotency.py::test_no_auth_passes_through_to_401`),
  so an auth failure can never be cached and replayed as if it had
  succeeded. Separately, `client_secret` appearing in a buyer's own
  `GET /v1/jobs/{id}`/accept-path response while `AWAITING_PAYMENT` is not
  a leak: it's buyer-facing by design (the client needs it to confirm the
  charge), scoped to the owning buyer only (see the payments update
  above), and it isn't a case of an idempotency response echoing a
  *different* principal's secret. The standing rule going forward: no
  endpoint should echo a secret belonging to a **different** principal
  into a stored idempotency response; audit any future secret-returning
  POST against that bar, not against "does it return a secret at all."

## Threat model (pilot)

Identity comes from an authenticated principal, never from a request body or
query param. Three roles: `buyer`, `seller`, `admin`, each resolved from an
`auth_sessions` row (see "Update — real-user auth" above) — not a shared
secret, not a caller-supplied claim. This is enough to give real users
distinct, unspoofable, revocable identities; the residuals above (no
rate-limiting, the reset-timing delta, verification gating nothing) are
named pilot-grade gaps, not silent ones. See `ROADMAP.md` for what stays
fork work (admin RBAC, gateway rate-limiting, OAuth/social login).

## Findings and status

All line references were against the v1 scaffold. "Fixed" means closed on the
hardening branch with a regression test in `tests/test_auth_and_hardening.py`.

### Critical — identity was a caller-supplied string
| ID | Finding | Status |
|----|---------|--------|
| C1 | `/admin/*` fully open — anyone could dump the both-sides ledger and re-tune pricing | **Fixed** — router gated behind `require_admin` |
| C2 | Seller could read `buyer_price` via `GET /jobs/{id}?role=buyer` | **Fixed** — buyer-role + ownership enforced; `role` param removed |
| C3 | Buyer could read `seller_payout` via `GET /jobs/offered?seller_id=X` | **Fixed** — seller derived from token, not query |
| C4 | Full seller impersonation on `accept`/`complete` | **Fixed** — seller derived from token |

### High
| ID | Finding | Status |
|----|---------|--------|
| H1 | Quote double-consume (TOCTOU) → two jobs from one quote | **Fixed** — module lock; concurrency test |
| H2 | Double-complete race → duplicate transaction | **Fixed** — locked status CAS; concurrency test |
| H3 | Unknown adjuster name in a pipeline → 500 on every quote | **Fixed** — validated against `REGISTRY` at config time (422) |
| H4 | Unbounded memory + no rate limit | **Partly fixed** — expired quotes swept on write, active-quote cap. Rate limiting deferred to the gateway (see ROADMAP) |
| H5 | `margin_floor` accepted NaN/negative/unbounded | **Fixed** — validated `MarginFloorBody` (finite, bounded) |

### Medium
| ID | Finding | Status |
|----|---------|--------|
| M1 | `inf` base prices passed `Field(gt=0)` | **Fixed** — `allow_inf_nan=False` |
| M2 | `adjuster_params` unbounded → negative/inf prices | **Fixed** — clamped at read time (`pricing._bounded`) |
| M3 | Floor enforced on unrounded values, booked on rounded | **Partly fixed** — round-then-check. Full `Decimal` migration deferred |
| M4 | `available_for` iterated a live dict → 500 | **Fixed** — `list()` snapshot |
| M5 | Rejection message + exact bump leaked min payout | **Fixed** — generic message; corrected price rounded up off the exact grid |
| M6 | New-buyer-discount farming via rotating `buyer_id` | **Fixed** — buyer identity is authenticated |
| M7 | No pagination on list endpoints | **Fixed** — `limit`/`offset` (capped) |

### Low / deferred
- **L1** ID-length caps — **Fixed** (`max_length` on body/id fields).
- **L2** `POST /jobs` bound no buyer — **Fixed** (quote ownership checked).
- **L3** No CORS/TrustedHost/security-header middleware — **Fixed** —
  `TrustedHostMiddleware`/`CORSMiddleware` wired, open by default
  (`TRUSTED_HOSTS=["*"]`, `CORS_ORIGINS=[]`), narrowed via env vars in
  production. See "Update — observability & ops".
- **N1** A hand-crafted non-compliant JSON body (`NaN`/`Infinity`) is
  correctly rejected (value never stored) but still surfaces as a 500 rather
  than a clean 422 — **Mitigated**: the global error envelope (see "Update —
  observability & ops") means that 500 is now a clean, request-id-bearing
  body with no traceback leak, not a raw crash. A non-standard client is
  still required to trigger it, and closing the gap to a clean 422 would
  need transport-level JSON strictness, not app-level validation — left
  open, low severity.

## Confirmed sound (not re-touched)
uuid4 IDs (unguessable) · no dynamic code execution from config (pure dict
lookups) · view-model field separation (`BuyerJobView`/`SellerJobView`,
`extra="forbid"`) — the old leaks were wrong-endpoint *access*, not field bleed ·
dependencies current, no known CVEs.
