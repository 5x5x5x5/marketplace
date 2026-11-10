# Security

This document describes the security posture of the template as it stands:
the threat model, how each control works and which test covers it, the risks
that remain, and the history of findings. The invariants that keep these
controls in place are in [`CLAUDE.md`](CLAUDE.md); several are also enforced
by `tests/test_architecture.py` and the Semgrep rules in `.semgrep/rules/`.

## Current posture

- **Identity** comes only from a DB-backed session resolved from a bearer
  header or an `HttpOnly` cookie, never from a request body. Session tokens,
  email-verification tokens and password-reset tokens are stored as sha256
  hashes; passwords as argon2id.
- **Each audience gets its own response models.** Buyers never receive the
  payout, sellers never receive the buyer price, the public catalog and
  pages receive neither, and dispute views split the same way.
- **Money paths** use database row locks, commit before the response is
  sent, use deterministic provider idempotency keys, and record refunds and
  chargebacks on an append-only ledger.
- **Payment webhooks** are signature-verified when Stripe is configured. In
  fake-provider mode the webhook route answers 404 unless explicitly opened
  for local use, and startup refuses unsafe combinations. Events are
  deduplicated by provider event id, failures go to a retry outbox that
  stores parsed fields only, and every event type is safe to replay.
- **Operational controls**: a clean 500 envelope with no internals, access
  logs without headers, bodies or query strings, a request body cap, trusted
  hosts and CORS settings (open by default, to be narrowed), and a 503 with
  `Retry-After` when the connection pool is exhausted.
- **Automated checks** run in CI: Semgrep (repo rules plus standard Python and
  FastAPI packs), a dependency audit of the lockfile, and gitleaks over the
  full git history.

What is **not** covered, stated plainly (details under
[Residual risks](#residual-risks)): no rate limiting anywhere in the app,
including the argon2 endpoints; email verification is off by default; login
CSRF is accepted; the browser payment step has not been run against Stripe;
no security response
headers (CSP, frame options, HSTS) set by the app; trusted hosts and CORS
ship open; row locks and pooled connections are held across payment-provider
calls; and one shared admin role with full authority.

## Threat model

Three roles, `buyer`, `seller` and `admin`, each resolved from an
`auth_sessions` row, not from a shared secret or a caller-supplied claim.
The assets are the two independently computed prices (each side must not
learn the other's number, because the spread is the business model), the
money moved through the payment provider, and user accounts.

In scope: an unauthenticated caller, an authenticated buyer or seller trying
to read the other side's data or act as someone else, forged or replayed
provider webhooks, replayed client requests, races between concurrent
requests, and hostile content submitted by sellers that renders on public
pages.

Out of scope for the app itself, and expected at the edge or in a fork:
volumetric abuse and brute force (rate limiting), TLS termination, bot
detection, admin separation of duties (there is one admin role), and abuse
heuristics. Where a control below is only effective with edge
configuration, it says so.

## Identity and sessions

- **Sessions are opaque bearer tokens, hashed at rest.**
  `POST /v1/auth/signup` and `POST /v1/auth/login` issue a random 32-byte
  token; only its sha256 is stored (`auth_sessions.token_hash`). A database
  dump does not contain usable tokens. Each request does one indexed lookup
  (`token_hash` and `expires_at > now`) to resolve `(role, user_id)`. The
  pilot HMAC tokens (`mint_token`, `MARKETPLACE_SECRET`) were deleted, and
  `tests/test_architecture.py` fails if token minting or a second identity
  verifier comes back.
- **Sessions are revocable and expire.** Logout deletes that session's row.
  A password reset deletes every session for the account, so a stolen token
  stops working when the owner resets. Sessions expire after
  `SESSION_TTL_HOURS` (default 72); expired rows are deleted by the
  maintenance loop's housekeeping. Suspension does **not** revoke sessions:
  a suspended user can still log in and read, by design (see
  [Moderation](#moderation-and-notification-floor)).
- **Passwords are argon2id** via `pwdlib` (`auth.hash_password`), never
  stored or logged in plaintext.
- **Login gives no account-existence signal.** An unknown email still runs
  a verify against a fixed dummy hash and returns the same 401 as a wrong
  password.
- **Password-reset request always answers 200 `{"status": "ok"}`**, whether
  or not the account exists. A timing difference remains (see residual
  risks).
- **Verification and reset tokens** are stored as sha256 only and are
  single-use (`used_at` set when consumed). Their mail is the one message
  sent directly rather than through the notification outbox, because the
  outbox would store the raw token. A failing mail sender is caught and
  logged, so it can neither fail nor fingerprint the request.
- **Accounts are one email plus one role**; `(email, role)` is unique.
  There is no admin signup: the admin is seeded at startup from
  `ADMIN_EMAIL`/`ADMIN_PASSWORD`, and changing the password and restarting
  replaces the hash and revokes existing admin sessions.

### Cookie posture

`POST /signup` and `POST /login` on the page routes (`pages/account.py`) set the
session token as a cookie instead of returning it:

- `HttpOnly`: page JavaScript can't read it. The app ships no client-side
  JavaScript of its own beyond a vendored copy of HTMX that no page uses yet.
- `SameSite=Lax`: sent on top-level navigation, not on cross-site
  subrequests or cross-site POSTs. This is the CSRF mitigation (below).
- `Secure` only when `COOKIE_SECURE=true`. It defaults to false for local
  HTTP; **a deployment behind TLS must set it**, or the cookie can be sent in
  the clear.
- **The cookie holds the raw session token**, the same value a bearer
  client holds, for the same `auth_sessions` row. Storage stays sha256-only,
  and logout and password reset revoke cookie sessions exactly as they
  revoke bearer sessions. There is no separate cookie-session table, TTL or
  revocation path.
- **Header first.** `auth._resolve_principal` tries the `Authorization`
  header and falls back to the `session_token` cookie only if the header
  resolved nothing.

`tests/test_architecture.py::test_session_cookie_is_httponly_and_samesite`
and `tests/test_live_server.py::test_cookie_only_auth_over_socket` cover
this.

### CSRF

Authenticated page forms carry a CSRF token derived from the session: the
sha256 of a fixed domain prefix and the raw session token from the HttpOnly
cookie. It needs no server secret and no stored state, changes with every
login, dies with the session row, and cannot be computed from the sha256
that `auth_sessions` stores. It is compared in constant time, and a missing
or wrong token fails closed with a 403 before the handler runs. An
architecture test walks every POST route in the pages package and fails if
one lacks the check and is not on a short, named allowlist.

`SameSite=Lax` alone is not relied on. A same-site origin, such as another
port or a subdomain, still receives the cookie; the browser try-out
demonstrated this by auto-submitting a cancel from a page on another port,
which the token refused.

The unauthenticated forms (signup, login, verify, password reset) have no
session to bind a token to. Login CSRF, where an attacker logs a victim
into the attacker's account, is an accepted residual risk.

Every page mutation is a POST; no GET changes state, and the links in
verification and reset emails render a form that POSTs the token, so
opening one (or a mail scanner fetching it) never consumes it. Logged-in
pages answer `Cache-Control: no-store`, and every page sends
`Referrer-Policy: same-origin`, so tokens in the address bar never leave in
a Referer header.

The idempotency middleware never stores responses for `/signup`, `/login`
or `/logout`, so a replay can't resend a `Set-Cookie`.

### The page login path is narrower than the API

`POST /login` rejects any role other than `buyer` or `seller` before calling
`auth.login`. Correct admin credentials submitted through the form get the
same generic 401 as a wrong password, with no signal that the role was the
problem. Admin access is JSON-API only (`POST /v1/auth/login` with
`role: "admin"`). Regression:
`tests/test_pages.py::test_login_page_rejects_admin_role`.

### Unauthenticated surface

`GET /healthz`; `/v1/auth/signup`, `login`, `verify`,
`password-reset/request` and `password-reset/confirm`; `/v1/catalog/*`;
`POST /v1/payments/webhook` (signature-verified, or 404 in fake mode); and
the pages `GET /`, `GET /listings/{id}`, `GET /sellers/{id}`, `/static/*`,
`GET/POST /signup`, `GET/POST /login` and `POST /logout`. The pages render
the same catalog views as `/v1/catalog/*` and call the same signup and login
code as `/v1/auth/*`; they expose nothing those endpoints don't. Every
route, including read-only pages, runs under `CommitRoute`, and an
import-time check fails if one doesn't.

### XSS boundary

Seller-supplied text (listing title and description, display names) renders
on pages anyone can open. Jinja2 autoescaping is on for `.html` templates;
no template uses `|safe` and none builds HTML strings from user data.
`tests/test_pages.py::test_xss_listing_title_escaped` covers element
content, and `::test_xss_listing_title_escaped_detail_and_seller` covers the
`<img alt=…>` attribute, which has different escaping rules. The
architecture tests, Semgrep rules and an agent edit hook all reject `|safe`
or disabled autoescape.

## Information asymmetry

- **Separate response models per audience** (`Buyer*`, `Seller*`,
  `Admin*`, `Catalog*` in `models.py`), never hand-built dicts or ORM rows.
  `tests/test_architecture.py` checks that role-scoped routes declare typed
  response models and never return the other side's fields;
  `tests/test_information_asymmetry.py` checks responses and notification
  payloads.
- **Public pages** are scanned as text:
  `tests/test_pages.py::test_page_source_asymmetry*` fails if the rendered
  HTML contains `seller_payout`, `margin`, `capacity`, `payments_ready` or an
  email.
- **`client_secret`** appears only in the owning buyer's job view, and only
  while the job is `awaiting_payment`. Admin job routes reuse the buyer job
  schema without that enrichment, so the field is always empty there, and no
  seller view has it.
- **The seller's completion receipt** carries only `job_id`,
  `seller_payout` and `completed_at` (finding F1 below).
- **Disputes**: the buyer view never carries the clawback amount, the seller
  view never carries the refund amount; only the admin view has both.
  `reason` and `status` are visible to all three.
- **Reviews**: the buyer sees their own rating aggregate (`GET /v1/profile`),
  not individual reviews. Each party can read their own job's reviews
  (rating, comment, no party ids; a hidden comment reads as null). Cross-job
  review lists are admin-only.
- **Reports**: the reporter's view carries `status` but never the admin's
  `resolution_note`.
- **Notification payloads** are snapshots taken at enqueue time; seller
  payloads never carry the buyer price, buyer payloads never carry the
  payout.

## Money paths

- **Concurrency is the database's job.** Quote consumption, job and offer
  transitions and capacity checks take `SELECT … FOR UPDATE` row locks. On
  SQLite writes are serialized, so the deterministic race tests hold on
  both; true-parallel tests run on Postgres (CI runs the suite on both).
- **Commit before respond.** `CommitRoute` commits the session before the
  response is sent; a commit failure is reported as a 500, never hidden
  behind a 200 (finding F2).
- **Refunds and voids** happen only through the buyer- or admin-owned cancel
  path and admin dispute resolution; there is no standalone refund endpoint.
- **Dispute resolution is admin-only and bounded.** `refund_amount` and
  `clawback_amount` are quantized and range-checked (0 to the buyer price,
  0 to the payout) before any provider call. Each provider leg has its own
  key (`refund:{job_id}:dispute`, `reversal:{job_id}:dispute`, distinct from
  the cancel path's keys). The amounts are committed before the provider is
  called, so a provider failure leaves them pinned and a retry converges on
  the same amounts. A partial refund never changes the charge's status.
  Opening a dispute requires the job's own buyer (404 otherwise), a
  completed job inside `DISPUTE_WINDOW_DAYS` and no existing dispute.
- **Provider idempotency keys** are derived from the job (`charge:{job_id}`,
  `transfer:{job_id}`, `refund:{job_id}`, `acct:{seller_id}`), so a retry
  replays the provider's original result.
- **Client idempotency.** Records are keyed on `(principal, key)`, so one
  caller can never replay another's response. 401, 403 and 5xx responses are
  never stored (`tests/test_idempotency.py::test_no_auth_passes_through_to_401`),
  so neither an auth failure nor a pool-exhaustion 503 can be replayed. The
  record is committed before the response is forwarded, so an immediate
  same-key retry replays rather than re-executing (finding F2b;
  `tests/test_live_server.py::test_same_key_immediate_retry_replays_byte_identical`).
  A failure to store the record is logged and swallowed, because the
  operation already committed
  (`tests/test_idempotency.py::test_store_failure_never_fails_a_committed_operation`).
  Standing rule: no endpoint may store a secret belonging to a different
  principal in an idempotency response. `client_secret` in the owning
  buyer's own response is not such a case.
- **Fee estimate.** `Payment.fee_estimate` is computed from the admin fee
  config and stamped once at charge time, never recomputed. The margin floor
  is enforced net of the estimate at quote time and when a seller is chosen.
  A fee-config change between quote and accept is applied at the stamp, the
  same eventual-consistency stance as the floor itself. Payments created
  before the fee migration carry 0.
- **Expired idempotency records.** A key replayed after
  `RETENTION_IDEMPOTENCY_DAYS` re-executes instead of replaying, but the
  money paths still hold: a repeat accept or decline on a resolved offer is a
  409, a consumed quote is a 404 or 410, and the charge carries its own
  provider key. Providers keep such keys only briefly (Stripe: about 24
  hours), so it is the state-machine guards, not the provider key, that hold
  at any age.

- **Money-path audit (2026-11).** Every provider call site and every
  pairing of row locks was checked against five classes: inverted lock
  order, stale-snapshot dispatch, a replayable 4xx after a provider side
  effect, retries that do not converge, and resurrection of a terminal
  state. Two findings were fixed with tests that failed first:
  - **A1 (medium).** The cancel paths locked the job and then wrote its open
    offers, while accept, decline and the offer sweep lock offer then job. A
    cancel racing an accept deadlocked on Postgres and one side got a 500.
    The cancel paths now claim offers `FOR UPDATE SKIP LOCKED`.
  - **A2 (critical).** A dispute clawback equal to the seller's whole payout
    fully reverses the transfer, which the provider reports like a failed
    transfer. The handler marked the payout FAILED and paged admins, and an
    admin payout retry would then have paid the seller again. A reversal
    covered by a pinned clawback now leaves the payout PAID.
  Six lesser hazards are recorded and deliberately left open (a
  millisecond-wide lock inversion that heals through the webhook outbox,
  the provider's roughly 24-hour idempotency-key window, unlocked completion
  counters, and three others). The lock-order table, the retry table and the
  paths found safe are in
  [`docs/money-path-audit-2026-11.md`](docs/money-path-audit-2026-11.md).

## Payment webhooks

- **Authenticity.** `POST /v1/payments/webhook` has no bearer token (the
  provider can't present one). With Stripe configured, `parse_webhook`
  verifies the provider's signature before anything is applied; an invalid
  or missing signature is a 400. Startup refuses `STRIPE_SECRET_KEY` without
  `STRIPE_WEBHOOK_SECRET`.
- **The unsigned endpoint is closed by default.** The fake provider can't
  verify anything and its object ids are guessable, so before this change a
  deployment without Stripe accepted forged payment events. Now, in fake
  mode, the route answers 404 unless `ALLOW_UNSIGNED_WEBHOOKS=true`, which is
  meant for local development, tests and the demo only. Startup refuses
  `ALLOW_UNSIGNED_WEBHOOKS` together with a Stripe key. Covered by
  `tests/test_webhook_gate.py`. The flag remains a configuration risk: a
  deployment that sets it without Stripe accepts forged events.
- **Deduplication.** Every event is recorded in `webhook_events` keyed on
  the provider's event id before it is applied. Any existing row (applied,
  failed or dead-lettered) makes a redelivery a `duplicate` no-op; recovering
  a failed event belongs to the retry job, not to a redelivery racing it. The
  row is flushed before the apply, so two simultaneous first deliveries meet
  at the unique constraint and the loser answers `duplicate`, not 500.
- **What the outbox stores.** When applying an event fails, the row is
  persisted as `failed` in a fresh session with the attempt count, a
  next-attempt time and a `last_error`, and the endpoint answers 200
  `{"status": "deferred"}`. The stored `payload` is the parsed event fields
  (event id, kind, object id and a few scalars), never the raw body or the
  signature header. `last_error` is the exception type plus the first line
  of its message, capped at 512 characters, because database driver errors
  put the SQL statement and its bound parameters on later lines; those stay
  in the server log and out of the table, the admin list endpoint and the
  dead-letter email (`tests/test_webhook_outbox.py::test_last_error_keeps_only_the_first_line`).
- **Retry and dead letter.** The maintenance loop claims due rows with
  `SKIP LOCKED` and re-applies each in its own savepoint, with exponential
  backoff, until it applies or reaches `WEBHOOK_MAX_ATTEMPTS`; then the row
  is dead-lettered and every admin gets `webhook_dead_letter_admin`, which
  can't be muted. Admins can list rows and re-queue one; listing and
  re-queueing require an admin session.
- **Replay safety does not depend on the dedup table.** Every event branch
  is safe to apply twice and safe to apply after later events for the same
  object (`tests/test_webhook_replay.py`): status lattices or transition
  guards for payments, payouts and accounts, and permanent rows for
  money-moving effects (a lost chargeback's fee row carries the provider's
  dispute id). This matters because applied webhook rows age out after
  `RETENTION_WEBHOOKS_DAYS`; a redelivery after that is applied again and
  must change nothing. A late success never revives a cancelled or refunded
  payment, and a late failure never downgrades a succeeded one.
- **Chargebacks** whose payment id doesn't match a known payment are
  recorded and ignored, not a 500. An admin's dispute resolution is kept as
  the dispute's status if a chargeback closes later; the loss and fee are
  still booked.

## Moderation and notification floor

- **Reports are limited to counterparties**: a user who shares a job with
  the reporter, or a review the reporter is a party to; otherwise 403, and a
  self-report is 422. One report per reporter and target, enforced by a
  unique constraint, so sequential and concurrent duplicates both get 409.
  Every report notifies every admin in the same transaction.
- **Resolving a report is terminal and inert**: a second resolve is 409, and
  resolving writes only to the report row. Any consequence is a separate
  admin action.
- **Suspension blocks acquisition only**: quotes, jobs, reviews, disputes,
  availability, accepting offers, payment onboarding and filing reports get
  `403 {"detail": "account suspended"}`. Login, reads, complete, decline and
  cancel stay open so in-flight work finishes; matching skips suspended
  sellers. Suspension never calls the payment provider. Admin accounts can't
  be suspended (422).
- **Takedown hides, never deletes.** Hiding a review comment removes the
  text from non-admin views while the rating still counts. Admin views show
  the raw comment and the hidden flag. Display-name reset is deterministic
  (`user-{id[:8]}`) and independent of takedown.
- **A server-side notification floor**: `refund_issued_buyer`,
  `dispute_resolved_buyer`, `dispute_resolved_seller`, `payout_failed_admin`
  and `webhook_dead_letter_admin` can't be muted, not through the
  preferences API (422) and not through a mute row inserted directly into
  the database, because the floor is checked at enqueue.

## Operational controls

- **Error envelope.** `RequestIdMiddleware` wraps every route. An unhandled
  exception becomes `{"detail": "internal error", "request_id": …}` with
  status 500 and no traceback or message in the body; the traceback goes to
  the log under the same request id. It is middleware rather than an
  exception handler so the request id survives on exactly these responses.
  404 and 422 keep FastAPI's shapes
  (`tests/test_observability.py::test_envelope_hides_internals`,
  `::test_http_exceptions_not_enveloped`).
- **Pool exhaustion sheds load.** When a request can't get a database
  connection within `DB_POOL_TIMEOUT_SECONDS` (default 5), the same
  middleware answers 503 `{"detail": "server busy, retry shortly"}` with
  `Retry-After: 1`, logs a warning, and the idempotency layer does not store
  it (`tests/test_capacity.py::test_pool_exhaustion_sheds_with_503_not_500`).
  This is load shedding, not a fault: the alternative was every request
  stalling for 30 seconds and failing with 500. A 503 under load is
  expected; sustained 503s mean the pool, the threadpool or the database is
  undersized (see [`docs/DEPLOY.md`](docs/DEPLOY.md)). The live-quote cap
  (`MAX_ACTIVE_QUOTES`) also answers 503 when reached.
- **Access logs** record method, path without its query string, status,
  duration and request id; never headers, cookies, bodies or query strings,
  because bearer tokens travel in headers and reset or verification tokens
  in bodies (`tests/test_observability.py::test_access_log_line_shape_and_redaction`;
  the architecture tests check log calls statically). `/healthz` is not
  logged.
- **Retention** deletes old `idempotency_keys`, applied or dead-lettered
  `webhook_events` and sent or failed `notifications` on the maintenance
  loop. Pending notifications and failed webhook events are never deleted,
  whatever their age; dropping either would silently lose a message or a
  payment event.
- **Body cap.** `MAX_BODY_BYTES` (default 1 MiB) is checked against a
  declared `Content-Length` and counted on chunked bodies; either way the
  answer is 413 before the body reaches a handler or the idempotency store.
- **Trusted hosts and CORS ship open**: `TRUSTED_HOSTS=["*"]` and
  `CORS_ORIGINS=[]` (no CORS headers). A deployment must set both. The
  template does not enforce this.

## Automated checks

- **`.github/workflows/security.yml`** runs on pull requests, pushes to
  `main`, weekly and on demand:
  - Semgrep: the repo's own rules in `.semgrep/rules/invariants.yml`
    (identity fields in request models, Stripe imports outside the adapter,
    unsafe template output, sensitive logging, non-constant-time secret
    comparison, raw credentials at rest, float money fields), which must
    first pass their own test corpus, plus the `p/python` and `p/fastapi`
    packs. One registry rule is excluded because it flags a log message on
    the word "password" when the call logs only an email.
  - `pip-audit` over the locked runtime dependencies.
  - gitleaks over the full git history, with a checksum-verified binary.
- **`.github/workflows/ci.yml`** runs the test suite on SQLite and
  Postgres 16 and the migrations up, down and up on both.
- **`tests/test_architecture.py`** encodes the statically checkable rules:
  no identity fields in request bodies, no token minting, hash-only
  credential columns, argon2 for passwords, cookie flags, per-audience field
  denylists, typed response models, `Decimal` money, provider access only
  through the port, autoescape, log calls without headers or bodies, and
  more.
- Workflow actions are pinned to commit SHAs, workflows default to
  read-only `contents` permission, and Dependabot is configured.

- **Background-agent workflows** (`.github/workflows/agent-implement.yml`,
  `agent-review.yml`). These run a coding agent with repository credentials,
  so their inputs are the threat: issue bodies, comments, pull-request
  descriptions and diffs are text written by other people.
  - Only users with write access can start a run. A job that holds no
    secrets checks the actor's permission through the collaborators API and,
    for a pull request, that the head is in this repository; the job holding
    the API key runs only if that passes. Bot senders are excluded.
  - The review workflow uses `pull_request`, never `pull_request_target`,
    skips forks, and reads `CLAUDE.md` and the reviewer definitions from the
    base branch so a pull request cannot rewrite its own rules.
  - Each job asks for the minimum permissions. The implementing agent can
    push a branch and open a pull request; the reviewing agent can read code
    and write comments. Neither can approve or merge, and tool allowlists
    exclude web access, `gh api`, force-push and history rewriting. Event
    text reaches scripts only through environment variables.
  - The agent's instructions declare issue and pull-request text untrusted.
    That is a mitigation, not a guarantee: branch protection (a required
    human approval, required checks, no bypass) is what actually stops an
    agent's change from reaching the default branch, and it has to be
    configured by the maintainer (`docs/background-agents.md`).
  - The API key is the only secret exposed to agent jobs. These workflows
    have not been executed; they need that secret first.

- **Ops MCP server** (`src/marketplace_mcp/`). It authenticates as an admin
  with an ordinary revocable session token taken from the environment, and
  talks only to `/v1/admin/*` over HTTP, so it can do what an admin can do
  through the API and nothing else; an architecture test checks it never
  imports the app or the database layer. Write tools are not registered
  unless `MARKETPLACE_MCP_ENABLE_WRITES` is set, and dispute resolution and
  job cancellation are not exposed at all. The token is never logged or
  returned in a tool result. The residual risk is prompt injection: listing
  titles, report bodies and similar user-written text flow into the agent's
  context through the read tools. That text is delimited with a per-value
  random marker and labeled as untrusted, and no tool derives an action from
  content, but an agent with write tools enabled can still be talked into
  misusing them. Keep writes off unless needed, prefer per-call approval in
  the client, and revoke the session when done.

## Residual risks

Named so they are not mistaken for oversights. None is mitigated inside the
app unless stated.

- **No rate limiting.** Nothing in the app throttles login, signup,
  password reset or any other endpoint. Password brute force is unthrottled,
  and the argon2 endpoints are the cheapest way to exhaust CPU and memory.
  Mitigation belongs at the gateway or edge
  ([`docs/DEPLOY.md`](docs/DEPLOY.md#edge-rate-limits)); left to a fork.
- **Email verification is off by default.** `REQUIRE_VERIFIED_EMAIL`
  requires a verified email to create a job, accept an offer or create a
  listing. It ships off because without a real mail sender a verification
  requirement proves nothing; a deployment with mail configured should turn
  it on.
- **Timing difference on `password-reset/request`.** Status and body are
  identical, but the existing-account path does more work (a token insert
  and a send attempt), so response time leaks a faint signal. Closing it
  needs equal work on the other path, which isn't built.
- **Login CSRF.** Authenticated forms carry a session-bound token; signup,
  login, verify and password reset cannot, and an attacker can log a victim
  into the attacker's own account.
- **The browser payment step is unverified against Stripe.** It loads
  Stripe's script from Stripe's origin and confirms the payment in the
  browser; settlement is still the signed webhook. A Content-Security-Policy
  that allows only that script origin is not set (see the next item).
- **No security response headers.** The app sets no `Content-Security-Policy`,
  `X-Frame-Options`/`frame-ancestors`, `Strict-Transport-Security` or
  `X-Content-Type-Options`. Set them at the edge or add middleware before
  serving pages publicly.
- **Trusted hosts, CORS and `COOKIE_SECURE` ship in development settings.**
  A deployment that doesn't change them accepts any `Host` header and sends
  the session cookie without `Secure`.
- **`ALLOW_UNSIGNED_WEBHOOKS` is a footgun.** Startup refuses it alongside a
  Stripe key, but a fake-provider deployment that sets it accepts forged
  payment events.
- **Row locks and pooled connections are held across payment-provider
  calls** in accept, complete and cancel. A slow provider makes those
  requests hold locks and a pool connection for the length of a network
  call, which narrows capacity and can turn provider slowness into 503s.
- **A commit failure after a provider call** leaves the provider briefly
  ahead of the database. The client gets a truthful 500, and the state
  converges on retry: charges and refunds through idempotency-key replay,
  voids through the idempotent cancel.
- **Simultaneous same-key requests** can both execute (the unique constraint
  keeps one record); the row locks keep the duplicate safe. This is not
  exactly-once.
- **A non-standard JSON body** (`NaN`, `Infinity`) is rejected and never
  stored but answers 500 (the clean envelope) instead of 422; fixing that
  needs a stricter JSON parser at the transport level. Low severity
  (finding N1).
- **`fee_estimate` is not the provider's actual fee**; reconciling against
  provider balance transactions is left to a fork.
- **One admin role with full authority.** Any admin can retune pricing,
  move money through dispute resolution and suspend users. Admin RBAC is
  left to a fork.
- **The fake provider must never serve real traffic.** Selection is by
  environment (`STRIPE_SECRET_KEY`) with no runtime switch, so this is a
  deployment risk, not a code path a caller can toggle.
- **Dependency advisories.** The first run of `pip-audit` against `uv.lock`
  (2026-11) reported published advisories against the pinned versions of
  `starlette`, `anyio`, `urllib3`, `idna`, `certifi` and `dnspython`. All
  were bumped to fixed releases and the audit was clean afterwards. That is
  a point-in-time result: the weekly security workflow is what keeps it
  true, so check its latest run for the current state.

## Findings history

### Safe-to-pilot sweep

A read-only sweep of the first scaffold. "Fixed" means closed with a
regression test in `tests/test_auth_and_hardening.py`. Line references in
the original sweep were against that scaffold and are not repeated.

**Critical: identity was a caller-supplied string**

| ID | Finding | Status |
|---|---|---|
| C1 | `/admin/*` open to anyone: the ledger with both prices, and pricing config | **Fixed**: admin router behind `require_admin` |
| C2 | A seller could read `buyer_price` via `GET /jobs/{id}?role=buyer` | **Fixed**: buyer role and ownership enforced; `role` parameter removed |
| C3 | A buyer could read `seller_payout` via `GET /jobs/offered?seller_id=X` | **Fixed**: seller taken from the token |
| C4 | Full seller impersonation on accept and complete | **Fixed**: seller taken from the token |

**High**

| ID | Finding | Status |
|---|---|---|
| H1 | Quote double-consume (TOCTOU): two jobs from one quote | **Fixed**: first with a process lock, later replaced by a row lock on the quote |
| H2 | Double-complete race: duplicate transaction | **Fixed**: locked status transition; concurrency test |
| H3 | Unknown adjuster name in a pipeline: 500 on every quote | **Fixed**: validated against the registry at config time (422) |
| H4 | Unbounded memory, no rate limit | **Partly fixed**: expired quotes are reaped and live quotes are capped. **Rate limiting is not in the app** (see residual risks) |
| H5 | `margin_floor` accepted NaN, negative or unbounded values | **Fixed**: validated, finite and bounded |

**Medium**

| ID | Finding | Status |
|---|---|---|
| M1 | Infinite base prices passed `Field(gt=0)` | **Fixed**: `allow_inf_nan=False` |
| M2 | Unbounded `adjuster_params` could produce negative or infinite prices | **Fixed**: clamped when read |
| M3 | Floor checked on unrounded values, booked on rounded ones | **Fixed**: round-then-check at first, then `Decimal` money throughout in the template build |
| M4 | `available_for` iterated a live dict: 500 | **Fixed**: snapshot |
| M5 | Rejection message and exact bump leaked the minimum payout | **Fixed**: generic message; corrected price rounded up off the exact grid |
| M6 | New-buyer discount farmed by rotating `buyer_id` | **Fixed**: buyer identity is authenticated |
| M7 | No pagination on list endpoints | **Fixed**: `limit`/`offset`, capped, applied in SQL |

**Low**

| ID | Finding | Status |
|---|---|---|
| L1 | No length caps on ids | **Fixed**: `max_length` on body and id fields |
| L2 | `POST /jobs` bound no buyer | **Fixed**: quote ownership checked |
| L3 | No CORS, trusted-host or security-header middleware | **Partly fixed**: `TrustedHostMiddleware` and `CORSMiddleware` are wired (open by default, narrowed by settings). **No security headers** are set (see residual risks) |
| N1 | A non-standard JSON body (`NaN`/`Infinity`) is rejected but answers 500 rather than 422 | **Mitigated**: the 500 is the clean envelope with no traceback. Still open at low severity |

### Live try-out findings (2026-07-16)

Found by driving a running server after the suite and reviews were green;
details in [`docs/tryout-findings-2026-07-16.md`](docs/tryout-findings-2026-07-16.md).

| ID | Finding | Status |
|---|---|---|
| F1 | The seller's completion receipt used the admin ledger model, exposing the buyer price and margin | **Fixed**: seller-scoped receipt |
| F2 | The session committed after the response was sent (read-your-writes race) | **Fixed**: `CommitRoute` commits before responding |
| F2b | The idempotency record was stored after the response, so an immediate retry could execute twice | **Fixed**: record committed before the response is forwarded |
| F3 | Documentation error in a dispute endpoint path | **Fixed** |
| F4 | Creation endpoints disagreed on 200 vs 201 | **Fixed**: creating POSTs return 201 |
| F5 | A cancel racing a complete could refund a completed job (stale read after a row lock) | **Fixed**: re-read under the lock |

### Later fixes

- **Forged webhooks without Stripe.** The fake provider's webhook accepted
  unsigned events in any deployment without Stripe. Closed by default (see
  [Payment webhooks](#payment-webhooks)).
- **Webhook replay.** An audit of every payment-event branch for second and
  late application fixed several cases where a retried or out-of-order event
  could re-notify, overwrite newer state, flip a failed payout back to paid,
  or book a lost chargeback twice.
- **Late payment success.** A webhook success arriving after a cancel could
  revive the payment; it is now ignored.

## Confirmed sound

uuid4 ids (unguessable) · no dynamic code execution from configuration
(adjusters and strategies are looked up by name in a registry) · response
model field separation: each view declares only its own fields and FastAPI
serializes through the declared model, so extra attributes on the source
object are dropped (the original leaks were access to the wrong endpoint,
not fields bleeding between models). An earlier version of this document
said the views used `extra="forbid"`; they don't, and the separation does
not depend on it.
