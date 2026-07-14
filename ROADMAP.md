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
gates nothing yet; see `SECURITY.md`).

## What's still ahead

Rough priority. Each is fork-agnostic — build generic here, specialize after forking.

1. **Notifications** — email/push on offered/accepted/completed (async). Now
   unblocked: every identity has a verified-or-not email (`User.email`) and
   the outbound-mail port already exists (`src/marketplace/mail.py`,
   currently backing verification/reset) — a real sender adapter and this
   phase share the same seam.
2. **Trust & safety** — disputes/chargebacks, partial refunds, seller→buyer
   reviews, fraud/abuse, moderation.
3. **Processing fees in the margin math** — the `Transaction` ledger records
   margin gross of provider fees (Stripe: ~2.9% + 30¢ per charge, and the fee
   is kept on refunds — a refunded job costs the platform the fee with no
   ledger entry). Verified on the test account: $50.00 ledger margin landed as
   $42.14 cash after $7.86 in fees. Either absorb expected fees in the margin
   floor or record a per-transaction fee estimate alongside the margin.
4. **Background scheduler** — replace the lazy offer-expiry / payment-timeout
   sweep with a cron/worker.
5. **Observability & ops** — metrics, structured request logging, an error
   envelope so a crafted body never surfaces a 500. Payments hardening
   follow-ups: the webhook handler is async-over-sync-`Session` (move DB work
   off the event loop under load); a TTL sweep for the `idempotency_keys` /
   `webhook_events` tables; a PG-gated cancel-vs-webhook race test; indexes on
   `provider_account_id`/`provider_transfer_id`.
6. **Admin RBAC** — beyond the single shared admin role; every admin account
   currently has identical, full authority.
7. **API hardening** — CORS/TrustedHost, gateway rate-limiting, request-size limits.
8. **OAuth / social login** — Google/GitHub sign-in alongside password auth.
   Fork-time item: real-user auth (password signup/login, sessions, argon2,
   verification/reset) already shipped — see "Done" above and `SECURITY.md`.
   This is additive, not a replacement.

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
