# Roadmap

Goal: a **generic two-sided marketplace template** that scales to hundreds of
users, then gets forked and specialized per market vertical. The differentiator
— platform sets buyer price and seller payout independently and keeps the spread
— stays in this repo's core; everything else is a swappable adapter.

## Where we are

- v1 scaffold: pricing pipelines + matching strategies + information asymmetry, in-memory.
- Safe-to-pilot hardening (this branch): real identity, concurrency locks,
  admin-input validation, resource caps. See `SECURITY.md`.

## What every marketplace of this shape still needs

Rough priority order. Each is a fork-agnostic capability — build it generic here,
specialize after forking.

1. **Persistence** — Postgres + migrations. In-memory resets on restart and
   blocks everything below. *Biggest single gap.*
2. **Payments & payouts** — Stripe Connect. The spread model maps directly to
   **destination charges**: the buyer's payment lands on the platform balance and
   a chosen amount transfers to the seller, so the platform keeps the spread by
   construction. Also: seller onboarding/KYC, refunds, escrow/holds.
3. **Lifecycle completeness** — cancellation (the `CANCELLED` status exists but
   has no endpoint), offer timeout + re-match, refunds.
4. **Trust & safety** — ratings/reviews write path (`SellerProfile.rating`
   exists, nothing writes it), disputes, fraud/abuse controls, moderation.
5. **Money correctness** — `Decimal` end-to-end, idempotency keys on money ops,
   immutable audit ledger.
6. **Notifications** — email/push on job offered/accepted/completed (async).
7. **Observability & ops** — structured logging, metrics, health/readiness,
   error handling that never 500s or leaks internals.
8. **Admin RBAC + operator tooling** — beyond the single shared operator token.
9. **API hardening** — versioning, CORS/TrustedHost, gateway rate-limiting,
   request-size limits.

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
- **Payments** — Stripe Connect, destination charges. Verify the current
  v2-accounts / Standard-Express-Custom deprecation status before wiring
  onboarding.
- **Lock-in traps** — hosted Sharetribe (can't take the backend), Supabase
  auth+DB coupling, Auth0/Clerk cliffs, RDS proprietary features.

*Infra figures are from mid-2026 secondary sources; confirm on each vendor's own
pricing page before committing — PaaS/DB/auth pricing shifted several times in
2025–26.*
