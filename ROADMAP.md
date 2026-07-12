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

## Done ✓

Persistence (Postgres/SQLAlchemy/Alembic) · Decimal money · lifecycle
completeness (cancel/decline/offer-timeout/re-match/graceful-expiry) · ratings
write-path feeding `highest_rated` · seller tier + capacity management · `/v1`
API versioning · admin audit log · token expiry.

## What's still ahead

Rough priority. Each is fork-agnostic — build generic here, specialize after forking.

1. **Payments & payouts** — Stripe Connect. The spread maps directly to
   **destination charges**: the buyer's payment lands on the platform balance and
   a chosen amount transfers to the seller, so the platform keeps the spread by
   construction. Plus seller onboarding/KYC, refunds, escrow/holds.
2. **Idempotency keys** on money-mutating POSTs (create-job, complete) — needs a
   key table + header handling; important once payments are real.
3. **Notifications** — email/push on offered/accepted/completed (async).
4. **Trust & safety** — disputes, seller→buyer reviews, fraud/abuse, moderation.
5. **Background scheduler** — replace the lazy offer-expiry sweep with a cron/worker.
6. **Observability & ops** — metrics, structured request logging, an error
   envelope so a crafted body never surfaces a 500.
7. **Admin RBAC** — beyond the single shared operator token.
8. **API hardening** — CORS/TrustedHost, gateway rate-limiting, request-size limits.
9. **Auth** — replace pilot HMAC with a real user store + provider (fastapi-users /
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
- **Payments** — Stripe Connect, destination charges. Verify the current
  v2-accounts / Standard-Express-Custom deprecation status before wiring
  onboarding.
- **Lock-in traps** — hosted Sharetribe (can't take the backend), Supabase
  auth+DB coupling, Auth0/Clerk cliffs, RDS proprietary features.

*Infra figures are from mid-2026 secondary sources; confirm on each vendor's own
pricing page before committing — PaaS/DB/auth pricing shifted several times in
2025–26.*
