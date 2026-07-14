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

## Done ✓

Persistence (Postgres/SQLAlchemy/Alembic) · Decimal money · lifecycle
completeness (cancel/decline/offer-timeout/re-match/graceful-expiry) · ratings
write-path feeding `highest_rated` · seller tier + capacity management · `/v1`
API versioning · admin audit log · token expiry · **payments & payouts**
(seller onboarding gate, escrow charge-at-accept/transfer-at-complete, async
payment via signed+deduped webhook, payment-timeout sweep, cancel void/refund,
admin payout retry — Stripe Connect controller-properties accounts, fake
provider for dev/tests; **verified end-to-end against a real Stripe test
account 2026-07-13**: onboarding, charge, signed webhook, and transfer all
worked first-contact with zero adapter changes) · **idempotency keys** (client
`Idempotency-Key` header on money-mutating POSTs, replayed per-principal).

## What's still ahead

Rough priority. Each is fork-agnostic — build generic here, specialize after forking.

1. **Notifications** — email/push on offered/accepted/completed (async).
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
6. **Admin RBAC** — beyond the single shared operator token.
7. **API hardening** — CORS/TrustedHost, gateway rate-limiting, request-size limits.
8. **Auth** — replace pilot HMAC with a real user store + provider (fastapi-users /
   Supabase Auth).

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
- **Auth** — fastapi-users (currently maintenance-mode; a successor is in
  progress) or Supabase Auth. Avoid Auth0/Clerk MAU pricing cliffs until
  enterprise SSO justifies them. Replaces the pilot HMAC tokens.
- **Payments** — implemented (Stripe Connect, controller-properties accounts).
  Wired against `stripe` 15.3.0's `.v1` namespace; verified end-to-end on a
  real Stripe test account 2026-07-13 (onboarding, charge, signed webhooks,
  transfer). Going live is a dashboard activation + `sk_live` key swap, not code.
- **Lock-in traps** — hosted Sharetribe (can't take the backend), Supabase
  auth+DB coupling, Auth0/Clerk cliffs, RDS proprietary features.

*Infra figures are from mid-2026 secondary sources; confirm on each vendor's own
pricing page before committing — PaaS/DB/auth pricing shifted several times in
2025–26.*
