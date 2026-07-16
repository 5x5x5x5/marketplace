# Roadmap

Goal: a **generic two-sided marketplace template** that scales to hundreds of
users, then gets forked and specialized per market vertical. The differentiator
— platform sets buyer price and seller payout independently and keeps the spread
— stays in this repo's core; everything else is a swappable adapter.

## Where we are

- v1 scaffold: pricing pipelines + matching strategies + information asymmetry.
- Safe-to-pilot hardening: real identity, concurrency, admin-input validation.
- **Full template build (done):** Postgres + Alembic behind DI sessions, Decimal
  money, the complete job/offer lifecycle (capacity, decline, timeout+re-match,
  cancel, history), buyer→seller ratings, admin seller-tier management, `/v1`
  versioning, token expiry, and an admin audit log. See `SECURITY.md`.
- **Payments (done):** escrow via a provider port (Stripe Connect + a
  deterministic fake), seller onboarding gate, signed/deduped webhooks,
  payment-timeout sweep, cancel void/refund, admin payout retry, and client
  idempotency keys. **Verified against a real Stripe test account 2026-07-13**:
  full escrow loop (connected-account onboarding → PaymentIntent → signed
  webhook → transfer) ran with zero adapter changes, plus the adversarial
  paths — card decline + same-PI recovery, buyer void (PI canceled at Stripe),
  admin refund (charge refunded at Stripe), transfer reversal → payout retry
  under a fresh idempotency key (new transfer, not a replay), and the
  payment-timeout sweep voiding a live PaymentIntent.
- **Auth (done):** pilot HMAC (`mint_token`/`MARKETPLACE_SECRET`) is deleted;
  real signup/login/logout/me/verify/password-reset over DB-backed, revocable
  sessions (`/v1/auth/*`). Separate accounts per role (`unique(email, role)`),
  argon2 password hashing, admin seeded from `ADMIN_EMAIL`/`ADMIN_PASSWORD` at
  startup, and an `EmailSender` port (console adapter for now) backing
  verification/reset. Residuals named in `SECURITY.md`: no login
  rate-limiting, a timing delta on `password-reset/request`, and email
  verification doesn't gate anything yet (no real mail sender to make the
  gate meaningful).
- **Disputes + partial refunds (done):** first sub-phase of the trust &
  safety bucket. Buyers dispute a completed job within `DISPUTE_WINDOW_DAYS`
  (default 7) of `completed_at`, one dispute per job; an admin arbitrates
  with independent `refund_amount`/`clawback_amount` amounts (partial Stripe
  refund + partial transfer reversal, bounds-checked, idempotent legs),
  booked onto an append-only `adjustments` ledger — `Transaction` rows and
  `Payment.status` stay untouched. `GET /v1/admin/margins/summary` reports
  gross AND net-of-adjustments margin. Stripe chargebacks
  (`charge.dispute.created`/`closed`) ride the same webhook into the same
  `disputes` table, recorded and notified rather than fought — evidence
  submission stays in the Stripe dashboard (fork work). See `SECURITY.md`.
- **Seller→buyer reviews (done):** second T&S sub-phase, a mirror table
  (`seller_reviews`, one review per job) feeding a display-only buyer
  rating aggregate — `GET /v1/profile` for the buyer, `GET /v1/admin/buyers`
  for admin. The two review directions are independent (no double-blind);
  the aggregate gates nothing, by design (revisit if ratings ever gate
  anything). The four disputes-carried minors are closed: race-safe dispute
  creation, the fake-provider seam documented, the dead charge-only dispute
  `related_id` fallback deleted, and a DB `CHECK` on `adjustments.amount`.
  See `SECURITY.md`.
- **Moderation (done):** third T&S sub-phase — suspension, takedown, and
  abuse reports (migration #7). Suspension is verb-gated, not a global
  freeze: acquisition verbs (quotes, jobs, reviews, disputes, availability,
  offer-accept, payments-onboard, report-filing) 403 for a suspended user,
  while login, GETs, complete, decline, and cancel stay open and matching
  drops suspended sellers — freeze-new/finish-in-flight by design, and
  suspension itself never moves money. Takedown hides, never deletes: a
  hidden comment vanishes from non-admin views but the rating, aggregates,
  and admin visibility (raw text + flag) are untouched; a suspended/flagged
  user's display name can be reset to `user-{id[:8]}`. Reports are
  counterparty-only (a shared job, or a party to the review), one per
  reporter per target ever, and resolving is terminal with no automatic
  action taken. See `SECURITY.md`.
- **Notification preferences (done):** fourth and final T&S sub-phase —
  per-kind mutes (migration #8). `GET/PUT /v1/notification-preferences` is
  role-scoped (a buyer only sees/sets buyer kinds, same for seller/admin) and
  replace-set (PUT sends the full muted list, race-safe via a row lock on the
  user). Enforcement is at `enqueue`, not at send, so a smuggled DB row can
  never resurrect muted mail. A server-side money floor
  (`REFUND_ISSUED_BUYER`, `DISPUTE_RESOLVED_BUYER`, `DISPUTE_RESOLVED_SELLER`,
  `PAYOUT_FAILED_ADMIN`) can never be muted, by any path. See `SECURITY.md`.
- **Fee-aware margin (done):** the `Transaction` ledger recorded margin gross
  of the payment provider's cut, so a refunded job cost the platform its fee
  with no ledger entry (verified on the test account: $50.00 ledger margin
  landed as $42.14 cash after $7.86 in fees). Admin-tunable platform config
  (`pct`/`fixed`, defaulting to Stripe's 2.9% + 30¢, `PUT
  /v1/admin/config/fees`, migration #9) now backs a `fee_estimate` snapshot
  stamped onto every charge at charge time (never recomputed later), and the
  margin floor is enforced net of that estimate at both quote time and
  match-time candidate filtering so a floor-priced job can't be signed at a
  loss for the fee config in force at quote/match time — a fee-config change
  between quote and accept is applied at the stamp, the same
  eventual-consistency stance as the margin floor itself.
  `GET /v1/admin/margins/summary` adds `fees_estimated` and
  `platform_margin_net_of_fees` — a cash view over SUCCEEDED/REFUNDED charges
  that matches what actually lands in the bank account. See `SECURITY.md`.
- **Observability & ops (done):** the fifth and final generic bucket.
  Every request carries a request id (`X-Request-ID` honored if present,
  always echoed) threaded through a contextvar so every log line — access
  and application — carries it; JSON logs by default, `LOG_FORMAT=plain`
  for local reading. A single request-boundary middleware turns any
  unhandled exception into a clean `{"detail": "internal error",
  "request_id": …}` 500 — traceback only in the log, never the response
  body. `GET /v1/admin/stats` gives an operator a one-call snapshot
  (jobs/payments/payouts/notifications/disputes/reports/users/quotes/
  retention counts, uptime). Retention sweeps age out `idempotency_keys`
  (7d), `webhook_events` (30d), and terminal SENT/FAILED `notifications`
  (30d) on the maintenance loop's own clock — PENDING outbox rows are
  never reaped, by design (they still need to send). The webhook
  handler's DB work moved off the event loop (`asyncio.to_thread`),
  closing the async-over-sync-`Session` gap, alongside a
  resurrection-guard fix (a late-arriving webhook success can never
  resurrect a voided/cancelled payment) and a PG-gated
  cancel-vs-webhook race test. API hardening adds a body-size cap
  (`MAX_BODY_BYTES`, default 1 MiB), `TrustedHostMiddleware` and
  `CORSMiddleware` (both open by default; narrow via `TRUSTED_HOSTS`/
  `CORS_ORIGINS` in production), and `limit`/`offset` pagination on the
  admin list endpoints, plus indexes on the hot query paths. Migration
  #10 (10 total). See `SECURITY.md`.

## Done ✓

Persistence (Postgres/SQLAlchemy/Alembic) · Decimal money · lifecycle
completeness (cancel/decline/offer-timeout/re-match/graceful-expiry) · ratings
write-path feeding `highest_rated` · seller tier + capacity management · `/v1`
API versioning · admin audit log · **payments & payouts** (seller onboarding
gate, escrow charge-at-accept/transfer-at-complete, async payment via
signed+deduped webhook, payment-timeout sweep, cancel void/refund, admin
payout retry — Stripe Connect controller-properties accounts, fake provider
for dev/tests; **verified end-to-end against a real Stripe test account
2026-07-13**: onboarding, charge, signed webhook, and transfer all worked
first-contact with zero adapter changes) · **idempotency keys** (client
`Idempotency-Key` header on money-mutating POSTs, replayed per-principal) ·
**real-user auth** (DB-backed, revocable, sha256-at-rest sessions replacing
pilot HMAC; separate buyer/seller/admin accounts; argon2 passwords; admin
bootstrap from env; email verification + password reset over an `EmailSender`
port — residuals: no login rate-limiting, a reset-timing delta, verification
gates nothing yet; see `SECURITY.md`) · **notifications + background
scheduler** (transactional outbox: lifecycle events — 14 kinds today —
enqueued inside the domain transaction with role-safe payload snapshots; drained with
retry/backoff by an in-process maintenance loop that also runs the sweeps on
a clock — offers, stale payments, and sessions now expire without traffic;
stdlib SMTP adapter behind `SMTP_HOST`, console adapter otherwise; admin
list/drain endpoints; an external-worker extraction needs no schema change) ·
**disputes + partial refunds** (buyer-initiated arbitration within
`DISPUTE_WINDOW_DAYS`, one dispute per job; admin resolves independent
refund/clawback amounts onto an append-only `adjustments` ledger; gross AND
net-of-adjustments margin reporting; Stripe chargebacks recorded into the
same table via the existing webhook, not fought — evidence submission is
fork work; see `SECURITY.md`) · **seller→buyer reviews** (mirror
`seller_reviews` table, one per job; display-only buyer rating aggregate via
`GET /v1/profile` and `GET /v1/admin/buyers`; independent of the
buyer→seller direction, no double-blind; see `SECURITY.md`) ·
**moderation** (verb-gated suspension excluding suspended sellers from
matching; hide-not-delete takedown of review comments plus display-name
reset; counterparty-only abuse reports with a terminal admin resolve and no
automatic action; migration #7; see `SECURITY.md`) · **notification
preferences** (role-scoped, replace-set `GET/PUT
/v1/notification-preferences`; per-kind mutes enforced at enqueue; a
server-side money floor that can't be muted by any path, including a
smuggled DB row; migration #8; see `SECURITY.md`) · **fee-aware margin**
(admin-tunable `pct`/`fixed` fee config defaulting to Stripe's 2.9% + 30¢;
`fee_estimate` stamped on every charge at charge time; margin floor enforced
net-of-fees at both the quote path and match-time filtering; `fees_estimated`
/`platform_margin_net_of_fees` cash-view fields on the margin summary;
migration #9; see `SECURITY.md`) · **observability & ops** (request-id
propagation and JSON access logging with a `plain` toggle; a single
request-boundary middleware giving a clean 500 envelope; `GET
/v1/admin/stats` operator snapshot; 7/30/30 retention sweeps over
idempotency keys/webhook events/terminal notifications with PENDING rows
never reaped; webhook DB work moved off the event loop plus a
resurrection-guard money fix and a PG-gated race test; body-size cap,
`TrustedHostMiddleware`/`CORSMiddleware`, admin-list pagination, and
hot-path indexes; migration #10; see `SECURITY.md`).

## What's still ahead

**The template is feature-complete.** Trust & safety (four sub-phases) and
observability & ops are both done — see "Where we are" and "Done ✓" above.
What remains below is fork work by maintainer decision (2026-07-15): genuinely
fork-specific, not a generic default this template should carry, so it
stays unscheduled rather than rough-prioritized.

1. **Admin RBAC** — beyond the single shared admin role; every admin account
   currently has identical, full authority.
2. **Gateway rate-limiting / API extras** — login/endpoint throttling, API
   keys, quotas: deliberately left to the gateway/edge layer rather than the
   app (see `SECURITY.md`'s no-rate-limiting residuals). Body-size caps,
   `TrustedHostMiddleware`, and `CORSMiddleware` are already in the app —
   see "Done ✓" above.
3. **OAuth / social login** — Google/GitHub sign-in alongside password auth.
   Additive, not a replacement — real-user auth (password signup/login,
   sessions, argon2, verification/reset) already shipped; see "Done ✓" above
   and `SECURITY.md`.

Also fork scope: push/SMS notification channels and digest emails (the
`EmailSender` port and the transactional outbox are the extension points),
and automatic abuse signals/limits (report-count thresholds, auto-suspend) —
fork-specific heuristics, not a generic default (see `CLAUDE.md`'s
non-goals).

## Build vs template: build

No mainstream marketplace template supports platform-set double-sided pricing.
Sharetribe, Medusa/MercurJS, Vendure, Dokan/WC Vendors, and CS-Cart all assume
**seller-set price + admin commission**. The spread model is exactly the thing
they don't ship, and it's this project's whole point. Rule of thumb: **own the
differentiator (pricing/matching), rent the commodity** (auth/payments/hosting)
behind thin adapters. There is no notable FastAPI marketplace starter — this
scaffold is already ahead of that field.

## Infrastructure: defer, shortlist noted

Practitioner consensus is to pick infra *after* a product exists; at hundreds of
users this is firmly pre-scale. Keep the core in portable FastAPI + vanilla
Postgres behind adapters so none of these choices lock you in. When ready:

- **Hosting** — Railway or Render (zero-ops, ~$5–25/mo). Hetzner + Coolify only
  if you want rock-bottom cost and will own server ops. Fly.io only if you need
  global edge (you don't yet).
- **DB** — Neon (serverless, branching) or Supabase (bundles auth; note the
  7-day inactivity pause on free projects).
- **Auth** — implemented in-house (DB-backed sessions, argon2, `/v1/auth/*`;
  see "Done" above and `SECURITY.md`) rather than rented, so there's no
  vendor lock-in on identity. A fork wanting OAuth/social login or managed
  user infra instead can layer fastapi-users (currently maintenance-mode; a
  successor is in progress) or Supabase Auth on top; avoid Auth0/Clerk MAU
  pricing cliffs until enterprise SSO justifies them.
- **Payments** — implemented (Stripe Connect, controller-properties accounts).
  Wired against `stripe` 15.3.0's `.v1` namespace; verified end-to-end on a
  real Stripe test account 2026-07-13 (onboarding, charge, signed webhooks,
  transfer). Going live is a dashboard activation + `sk_live` key swap, not code.
- **Lock-in traps** — hosted Sharetribe (can't take the backend), Supabase
  auth+DB coupling, Auth0/Clerk cliffs, RDS proprietary features.

*Infra figures are from mid-2026 secondary sources; confirm on each vendor's own
pricing page before committing — PaaS/DB/auth pricing shifted several times in
2025–26.*
