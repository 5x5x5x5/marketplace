# Roadmap

What shipped, in order, followed by what is deliberately left to whoever
adapts this template and the known limitations of what is here. Each phase
from payments onward has a design spec and an implementation plan in
[`docs/superpowers/`](docs/superpowers/), named by date. The invariants each
phase added are in [`CLAUDE.md`](CLAUDE.md); security-relevant changes are in
[`SECURITY.md`](SECURITY.md).

## What shipped

### 0. Scaffold and safe-to-pilot hardening (2026-04 to 2026-07-11)

The first version: independent buyer and seller pricing pipelines,
pluggable matching strategies, and separate buyer and seller response models.
A read-only security sweep then found that identity was a caller-supplied
string (anyone could read the other side's price, act as any seller, or open
the admin API) along with race, validation and pagination problems. The
hardening pass gated every route on an authenticated principal (pilot-grade
HMAC tokens at the time), added concurrency guards and admin-input
validation. The findings and their status are in `SECURITY.md`.

### 1. Template build (2026-07-11)

State moved to Postgres behind SQLAlchemy sessions and Alembic
(`initial_schema`), with SQLite kept for local work and tests. Money became
`Decimal`. The job lifecycle was completed: seller capacity, decline, offer
timeout with re-match to the next seller, graceful expiry, cancel and
history. Buyer-to-seller ratings feed the `highest_rated` strategy. Admins
manage seller tiers and capacity. The API moved under `/v1`, tokens gained
expiry, and admin actions are written to an audit log. Concurrency moved to
database row locks, with a true-parallel capacity race test that runs on
Postgres.

### 2. Payments (2026-07-12 to 07-13)

Escrow through a provider port, with a deterministic fake and a Stripe
Connect adapter (`payments`). The buyer is charged when the seller accepts
and the payout is transferred at completion. Sellers must onboard before
matching offers them work. Charges that don't clear inline wait in
`awaiting_payment` until a signed, deduplicated webhook settles them, and a
sweep releases charges that never settle. Cancel voids or refunds. Admins can
retry failed payouts. Clients can send an `Idempotency-Key` on POSTs. The
commit history records a manual run against a Stripe test account on
2026-07-13: onboarding, charge, signed webhook and transfer, plus card
decline and recovery, void, refund, transfer reversal with payout retry, and
the payment-timeout sweep.

### 3. Real-user auth (2026-07-13 to 07-14)

The pilot HMAC tokens were deleted. Users sign up and log in with argon2
passwords and get opaque, revocable, DB-backed sessions stored as sha256
hashes (`auth`). Accounts are one email per role. The admin is seeded from
the environment at startup. Email verification and password reset go
through an `EmailSender` port with a console adapter.

### 4. Notifications and the maintenance loop (2026-07-14)

A transactional outbox (`notifications`): lifecycle events are enqueued in
the same transaction as the state change, with payloads that respect the
asymmetry, and an in-process maintenance loop sends them with retry and
backoff. The same loop runs the sweeps on a clock, so offers, stale payments
and sessions expire without traffic. An SMTP adapter is used when
`SMTP_HOST` is set. Admins can list and drain the outbox.

### 5. Disputes and partial refunds (2026-07-14)

Buyers can dispute a completed job within a window, once per job. Admins
resolve with independent refund and clawback amounts, booked on an
append-only `adjustments` ledger without editing transactions (`disputes`).
The margin summary reports gross and net-of-adjustments margin. Provider
chargebacks arrive through the same webhook into the same table and are
recorded rather than contested.

### 6. Seller-to-buyer reviews (2026-07-14)

A mirror review table, one per job (`seller_reviews`), feeding a buyer rating
aggregate that is displayed and gates nothing. Dispute creation was made
race-safe, and the adjustments ledger got a database `CHECK` on amounts.

### 7. Moderation (2026-07-14)

Suspension that blocks acquisition actions while letting in-flight work
finish and moves no money; takedown that hides review comments without
deleting them or touching ratings; display-name reset; and counterparty-only
abuse reports with a terminal, inert resolve (`moderation`).

### 8. Notification preferences (2026-07-14 to 07-15)

Per-kind mutes, scoped to the caller's role and set as a whole list
(`notification_prefs`). Mutes are enforced at enqueue, and a fixed set of
money-related kinds can't be muted by any path, including a row inserted
directly into the database.

### 9. Fee-aware margin (2026-07-15)

The margin ledger had been gross of the payment provider's fee; the spec
records a test-account run where $50.00 of ledger margin landed as $42.14 in
cash. An admin-tunable fee estimate (`pct`/`fixed`) is now stamped on every
charge when it is made and never recomputed (`fee_aware_margin`), the margin
floor is enforced net of that estimate at quote time and when choosing a
seller, and the margin summary adds `fees_estimated` and
`platform_margin_net_of_fees`.

### 10. Observability and operations (2026-07-15 to 07-16)

Request ids on every request and log line, JSON logs, a single middleware
that turns unhandled exceptions into a clean 500 envelope, `GET
/v1/admin/stats`, retention sweeps that never delete pending notifications,
webhook database work moved off the event loop, a fix so a late payment
success can't revive a cancelled payment, a body-size cap,
`TrustedHostMiddleware` and `CORSMiddleware`, admin list pagination, and
indexes on hot queries (`ops_indexes`).

### Try-out round (2026-07-16)

The template was driven by hand against a live server after the suite and
reviews were green. That added `scripts/seed.py`, bearer auth in Swagger and
the ten-minute walkthrough, and found five bugs that `TestClient` could not
show ([`docs/tryout-findings-2026-07-16.md`](docs/tryout-findings-2026-07-16.md)),
all fixed:

- F1: the seller's completion receipt returned the admin ledger model,
  showing the buyer price and margin to the seller.
- F2: the session committed after the response was sent, so a client's next
  request could miss its own write. Every router now commits through
  `CommitRoute` before responding.
- F2b: the idempotency replay record was stored after the response, so an
  immediate retry could execute twice.
- F4: creation endpoints disagreed on 200 versus 201.
- F5: a cancel racing a complete could refund a completed job (a stale read
  after taking a row lock).

### 11. Browse: listings, public catalog, pinned booking (2026-07-16 to 07-17)

Sellers publish listings with no price (`listings`); admins can hide them
independently of the seller's own on/off switch. An unauthenticated catalog
(`/v1/catalog/*`) shows listings with a live indicative `price_from` and no
payout, capacity, email or payment status. A quote created from a listing is
pinned to that seller and never moves to another seller on decline or
timeout. Quote and offer timeouts default to 15 and 120 minutes for a
person-paced flow. The demo and seed script exercise it.

### 12. Server-rendered pages and cookie sessions (2026-07-17)

Jinja pages for the catalog, listing detail and seller profile, plus signup,
login and logout pages that keep the session in an `HttpOnly`,
`SameSite=Lax` cookie backed by the same `auth_sessions` rows as the API
(buyer and seller only; no browser login for admins). Catalog reads were
extracted into `catalog.py` so the JSON API and the pages share one
implementation, after an earlier draft that duplicated the visibility check
let the two drift. The commit-before-response check was widened from `/v1/*`
to every route. Booking pages and CSRF tokens came later, in phase 14. No
migration.

### 13. Operational hardening, tooling and agent setup (2026-11)

- **Migrations from empty on SQLite.** The listings migration failed on an
  empty SQLite database; it was fixed in batch mode without changing
  existing Postgres schemas, and `tests/test_migrations.py` now runs the
  chain up, down and up again and checks for a single head.
- **Load-test harness.** Locust scenarios for browsing, the booking
  lifecycle and login, with a staged runner that records run conditions
  (`loadtest/`).
- **Capacity work.** The connection pool is sized against the threadpool
  (10 + 30 by default) and pool exhaustion answers 503 with `Retry-After`
  instead of a 30-second stall and a 500. List endpoints paginate in SQL.
  The quote path loads candidates in a fixed number of queries and its hot
  counts are indexed (`hot_path_indexes`). Deletes that nothing observes
  moved off read paths into housekeeping, the offer-expiry sweep claims rows
  with `SKIP LOCKED`, and `MAINTENANCE_ENABLED` lets one replica run the
  loop. Idempotency middleware database work moved off the event loop.
- **Webhooks.** The fake provider's unsigned webhook is closed unless
  `ALLOW_UNSIGNED_WEBHOOKS=true`, and startup refuses incomplete or unsafe
  Stripe configuration. Failed webhook handling is persisted to an outbox
  and retried with backoff, then dead-lettered with an admin notification;
  admins can list and re-queue events (`webhook_outbox`). Every payment-event
  branch was audited and fixed to be safe to apply twice and to apply late
  (`tests/test_webhook_replay.py`).
- **Tooling.** CI (lint, types, tests on SQLite and Postgres, migrations on
  both), a security workflow (Semgrep with repo rules, `pip-audit`, gitleaks
  over history), Dependabot, pre-commit hooks, a `justfile`, and
  `tests/test_architecture.py`, which checks the statically checkable
  invariants from `CLAUDE.md`.
- **Agent setup.** Skills, narrow reviewer subagents and hooks under
  `.claude/`, `AGENTS.md`, and [`docs/agent-setup.md`](docs/agent-setup.md).

- **Money-path audit.** Every provider call site and lock pairing checked
  against five bug classes (inverted lock order, stale-snapshot dispatch, a
  replayable 4xx after a side effect, retries that do not converge,
  resurrection). Two bugs fixed with tests that failed first: a cancel
  racing an accept could deadlock on Postgres, and a full dispute clawback
  was read back as a failed transfer, inviting a second payout. Six lesser
  hazards are recorded and left open in
  [`docs/money-path-audit-2026-11.md`](docs/money-path-audit-2026-11.md).

- **Services and routers.** `api.py` (about 2,800 lines holding the app,
  every JSON router and all domain logic) became app assembly only. Domain
  logic moved to `services/` (no web-framework imports; domain errors mapped
  to HTTP in one place) and the JSON routers to `routers/`. The OpenAPI
  document and the route list are identical before and after. This is what
  lets the pages reach the booking logic without importing the API.

- **Operating and maintaining it with agents.** An MCP server
  (`src/marketplace_mcp/`) exposes the admin API as operator tools, with
  write tools off by default and money-moving decisions left out. GitHub
  workflows let a maintainer hand an issue to an agent, give every pull
  request an agent review that can only comment, and run weekly upkeep
  ([`docs/background-agents.md`](docs/background-agents.md)); the agent
  workflows are unexecuted until a maintainer adds the API key secret.
- **Capacity, measured.** A before/after load-test pair is recorded in
  [`docs/capacity-2026-11.md`](docs/capacity-2026-11.md).

### 14. Booking and payment pages (2026-11-10)

Spec and plan: `docs/superpowers/specs/2026-11-10-booking-pages-design.md`,
`docs/superpowers/plans/2026-11-10-booking-pages.md`. A buyer can get a
price for a listing, confirm a booking, follow it, pay when the charge
needs confirming, and cancel; a seller has a dashboard for offers, jobs,
listings, availability and payments onboarding. The pages became a package
and call the same services as the JSON API. Every authenticated page form
carries a CSRF token derived from the session, with no server secret.
`REQUIRE_VERIFIED_EMAIL` (default off) gates booking, accepting and listing
in the services. The verification and password-reset links have pages. A
browser try-out (`docs/tryout-findings-2026-11-10.md`) found that catalog
cards had never linked to their listings, so nobody could have booked from
the browser; fixed. No migration.

Not verified: the Stripe Payment Element flow against a live account.

## Next

Nothing is in progress. The most useful next pieces of work, in order:

- **Run the Stripe path for real.** The card-entry step, the redirect back
  and the settling webhook have only been tested against the fake provider
  and the adapter's unit tests.
- **Stop holding locks across provider calls** (see Known limitations). The
  money-path audit lists what a safe restructuring has to preserve.
- **Close the gaps the reviewer evaluation found**
  (`evals/reviewer/results/2026-11-10/NOTES.md`): a test that every stored
  notification payload is role-safe, `buyer_id` in the architecture test's
  denylist for seller views, and a per-test timeout so a hang fails with a
  name.
- **Work through the audit's six deferred hazards**
  (`docs/money-path-audit-2026-11.md`).
- **Execute the agent workflows once** with the API key secret and branch
  protection in place, and fix what the first real run shows.

## Deliberately left to a fork

Decided 2026-07-15: these are specific to a particular marketplace, not
defaults a template should carry.

- **Gateway rate limiting**, API keys and quotas. The argon2 endpoints
  (signup, login, password reset, and the page forms that call them) most
  need a limit; [`docs/DEPLOY.md`](docs/DEPLOY.md#edge-rate-limits) suggests
  edge rules.
- **Admin roles.** Every admin account has the same full authority.
- **OAuth / social login**, alongside the existing password auth.
- **Push/SMS channels and notification digests.** The `EmailSender` port and
  the outbox are the extension points.
- **Automatic abuse signals** such as report-count thresholds or
  auto-suspend.

Also left to a fork, as noted where each comes up in the code and specs:

- **Chargeback evidence submission**; the app records chargeback outcomes
  only.
- **Reconciling actual provider fees** against the stamped estimate.
- **Multiple currencies**; one currency is configured.

Seller bidding is out entirely: this is a managed marketplace, not an
auction.

## Known limitations

- **Row locks and a pooled connection are held across payment-provider
  calls** in accept, complete and cancel. The fake provider answers
  instantly, so tests and the load harness understate this; with a real
  provider every accept or complete holds its locks and a pool connection
  for the length of a network call.
- **Email verification is off by default.** `REQUIRE_VERIFIED_EMAIL` gates
  booking, accepting and listing when set; without it `email_verified` is
  recorded and nothing checks it.
- **No rate limiting on the argon2 endpoints.** Each login, signup or reset
  attempt costs real CPU and memory; this has to be handled at the edge.
- **Load-test numbers come from a single shared box**: Locust, the app and
  Postgres on one machine. They are useful for comparing two builds, not as
  production capacity figures.
- **The browser payment step is unverified against Stripe.** The Payment
  Element is mounted and confirmed by a small script written from Stripe's
  documentation; it has not been run against a live or test-mode account.
- **Login CSRF is accepted.** Authenticated forms carry a session-bound
  token; the unauthenticated ones (signup, login, verify, password reset)
  have no session to bind to.
- **The Stripe adapter is tested for webhook signature verification and
  event mapping only**; the end-to-end Stripe run is a manual record from
  2026-07-13, not something the suite repeats.
- **A timing difference on `password-reset/request`.** Status and body are
  identical whether or not the account exists, but the existing-account
  path does more work.
- **A commit failure after a provider call** leaves the provider briefly
  ahead of the database; the client gets a truthful 500 and a retry
  converges through the provider idempotency keys.
- **Truly simultaneous requests with the same `Idempotency-Key`** can both
  execute; the row locks downstream keep that safe, but it is not
  exactly-once.
- **A non-standard JSON body** (`NaN`, `Infinity`) is rejected but answers
  500 (the clean envelope) instead of 422.
- **`Payment.fee_estimate` is an estimate**, not the provider's actual fee.

See `SECURITY.md` for the security residuals in more detail.
