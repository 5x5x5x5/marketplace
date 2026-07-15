# marketplace

A generic **two-sided marketplace** template. The platform sets the buyer-facing
price and the seller payout **independently** and keeps the spread — a *managed*
marketplace, not commission-on-seller-price. Pricing pipelines, matching
strategy, margin floor, and adjuster parameters are all tunable at runtime via
the admin API. Fork it and specialize it per market.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for local/tests)
· DB-backed session auth (argon2 password hashing via `pwdlib`). Money is
`Decimal`, serialized as JSON strings. Pyright strict across `src/` and `tests/`.

## Mechanism

1. Seller posts availability for a service type (and has a **capacity** — how many
   jobs they can hold at once).
2. Buyer requests a quote. The platform runs a buyer-side adjuster pipeline over a
   base price, and probes seller-side payouts; if the implied margin would fall
   below the configured floor the buyer price is bumped up (or the quote is
   rejected if the bump exceeds a ceiling).
3. Buyer creates a job from the quote. The matching strategy picks an eligible
   seller (spare capacity, clears the floor) and sends them an **offer**. If none
   fit, the job is returned `expired` — no silent dead-ends.
4. Seller accepts (→ job `accepted`) or declines; on decline or offer timeout the
   job re-matches to the next candidate. On accept the seller's capacity is
   consumed under a row lock.
5. Seller completes → transaction booked with `margin = buyer_price − seller_payout`,
   capacity freed. The buyer can then review the seller (feeds `highest_rated`).

Buyer- and seller-facing responses use distinct view models that exclude the
other side's number; only admin endpoints see both.

## Quickstart

```bash
uv sync

# Lint / types / tests — runs on SQLite, no Docker needed
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest

# See the whole lifecycle headless
uv run python scripts/demo.py

# Run the API locally (SQLite by default)
uv run uvicorn marketplace.api:app --reload
```

Against Postgres:

```bash
cp .env.example .env          # DATABASE_URL points at the compose Postgres
docker compose up -d db
uv run alembic upgrade head   # apply migrations (SQLite dev auto-creates tables)
uv run uvicorn marketplace.api:app --reload
```

The test suite runs against whatever `DATABASE_URL` points at, so `DATABASE_URL=postgresql+psycopg://… uv run pytest` exercises the same suite on Postgres (where the `FOR UPDATE` locks are real).

## Auth

Real users, not minted tokens. Signup creates a password-holding `User` row and
an opaque bearer session; every request after that carries `Authorization:
Bearer <token>`, resolved to `(role, user_id)` by one indexed lookup against
`auth_sessions`. Identity is taken from the resolved session, **never** from the
request body. Sessions are revocable (logout, password reset, and a future ban
path all just delete rows) and expire after `SESSION_TTL_HOURS` (default 72).
`GET /healthz` and `POST /v1/payments/webhook` are the only unauthenticated
routes. See `SECURITY.md`.

An **account is one email + one role**: the same email can hold a separate buyer
account and a separate seller account (each with its own password), but never
two accounts in the same role. There is no self-serve admin signup — the admin
account is seeded at startup from `ADMIN_EMAIL`/`ADMIN_PASSWORD` (see
`.env.example`); leave them unset and no admin account exists. Changing
`ADMIN_PASSWORD` and restarting **rotates** the admin credential: the stored
hash is replaced and existing admin sessions are revoked.

```python
import httpx
r = httpx.post(f"{BASE}/v1/auth/signup", json={
    "email": "alice@example.com", "password": "correct-horse-battery",
    "role": "buyer", "display_name": "Alice",
})
headers = {"Authorization": f"Bearer {r.json()['token']}"}
```

**`/v1/auth`** — `POST /signup` (201, returns a session) · `POST /login` ·
`POST /logout` · `GET /me` · `POST /verify` (email verification token) ·
`POST /password-reset/request` · `POST /password-reset/confirm` (revokes every
session on that account).

Outbound mail (verification/reset links) goes through an `EmailSender` port
(`src/marketplace/mail.py`); the shipped adapter just logs the message, so
links land in the server log until a fork plugs in a real sender (SES, Resend,
Postmark, …). The links themselves (`BASE_URL/verify?token=…`,
`BASE_URL/password-reset/confirm?token=…`) must be fronted by the fork's own
app — the API has **no GET handlers** for those paths; the token rides the
query string into whatever serves `BASE_URL`, which then relays it to
`POST /v1/auth/verify` or `POST /v1/auth/password-reset/confirm`. Email
verification doesn't gate anything yet — signup and login
both work on an unverified account — because there's no real sender to make a
verification requirement meaningful. A fork wires that gate in alongside its
mail adapter.

## Endpoints (all under `/v1`)

**Auth** (`/v1/auth/…`) — see the Auth section above.

**Buyer** — `POST /quotes` · `POST /jobs` · `GET /jobs` · `GET /jobs/{id}` ·
`POST /jobs/{id}/cancel` · `GET /profile` (own rating aggregate) ·
`POST /jobs/{id}/review` · `GET /jobs/{id}/reviews` (own job's review(s),
no party ids) · `POST /reports` · `GET /reports` (own filed
reports; shared with seller, see Seller list)

**Seller** (`/v1/seller/…`) — `PUT|GET /profile` (own capacity) ·
`POST /payments/onboard` · `POST|DELETE /availability[/{service_type_id}]` ·
`GET /offers` · `GET /jobs` · `POST /offers/{id}/accept` ·
`POST /offers/{id}/decline` · `POST /jobs/{id}/complete` ·
`POST /jobs/{id}/review` (rate the buyer) ·
`GET /jobs/{id}/reviews` (own job's review(s), no party ids) ·
`POST /v1/reports` ·
`GET /v1/reports` (shared with buyer, not `/seller`-prefixed — see Buyer list)

**Admin** (`/v1/admin/…`) — `GET /config` · `PUT /config/service_types/{id}` ·
`PUT /config/pipelines/{id}` · `PUT /config/margin_floor` ·
`PUT /config/matching_strategy` · `PUT /config/adjuster_params/{name}` ·
`PUT /config/fees` (pct/fixed provider-fee estimate) ·
`PUT /sellers/{id}` (tier/capacity) · `GET /buyers` (rating aggregates) ·
`GET /transactions` · `GET /payouts` ·
`POST /payouts/{id}/retry` · `GET /notifications` ·
`POST /notifications/drain` · `GET /margins/summary` · `GET /audit` ·
`GET /jobs` · `POST /jobs/{id}/cancel` · `POST /jobs/sweep` ·
`POST /users/{id}/suspend` · `POST /users/{id}/reinstate` ·
`POST /users/{id}/reset_display_name` · `GET /reviews/{kind}` (`buyer`|`seller`) ·
`POST /reviews/{kind}/{id}/hide` · `POST /reviews/{kind}/{id}/unhide` ·
`GET /reports` · `POST /reports/{id}/resolve`

**Payments** — `POST /payments/webhook` (provider event sink, unauthenticated,
signature-verified)

**Disputes** — Buyer: `POST /jobs/{id}/dispute` · `GET /jobs/{id}/dispute`.
Seller: `GET /jobs/{id}/dispute`. Admin: `GET /disputes` ·
`POST /disputes/{id}/resolve`

**Notification preferences** (`/v1/notification-preferences`, all roles) —
`GET` · `PUT` (replace-set; scoped to the authenticated principal's own
role-eligible kinds — a buyer can't see or mute seller/admin kinds)

## Job & offer state machine

- **Job**: `pending → awaiting_payment? → accepted → completed`; plus `expired`
  (no seller took it, or payment never arrived) and `cancelled`. A job only
  parks in `awaiting_payment` when the provider's charge doesn't clear inline.
- **Offer**: `offered → accepted | declined | expired`. Expiry/decline re-match to
  the next eligible seller (lazy sweep on reads + `POST /admin/jobs/sweep`).

## Payments

Escrow, not commission: accepting an offer charges the buyer and the money lands
on the platform's balance; completing the job transfers `seller_payout` to the
seller. The spread stays on the platform by construction — it's just the
difference between the two amounts, not a Stripe `application_fee`.

Provider selection is env-driven: unset `STRIPE_SECRET_KEY` and the app uses a
deterministic in-memory fake (instant-succeeding charges, scriptable for
dev/tests); set it and the app talks to Stripe (Connect, controller-properties
accounts) instead. Never run the Stripe adapter against a live account from this
template — it's unit-tested for signature verification only.

If a charge doesn't clear inline (the real-world case), the job holds in
`awaiting_payment` — the seller's capacity slot is held, and the buyer view
carries `client_secret` to confirm client-side. The provider's webhook
(`POST /v1/payments/webhook`) then moves the job to `accepted`. Unconfirmed
charges past `PAYMENT_TTL_MINUTES` are expired by the same sweep that expires
offers; a job whose payment already succeeded is never swept.

Money-mutating client POSTs (`/jobs`, `/offers/{id}/accept`,
`/jobs/{id}/complete`, …) accept an `Idempotency-Key` header — the first
response is replayed byte-for-byte on a retry with the same key, scoped per
authenticated principal.

Configuring a real Stripe account: point its webhook at
`POST /v1/payments/webhook` and set `STRIPE_WEBHOOK_SECRET` to verify signatures.

## Notifications

Seven lifecycle events email the right party: the seller's new-offer alert
(the one that makes 2-minute offer TTLs livable), the buyer's
accepted/payment-due, completed, expired, and refund notices, a cancel notice
to a committed seller, and a payout-failure alert to every admin.

Delivery is a **transactional outbox**: the state transition and its
`notifications` row commit in the same transaction (a rolled-back accept never
emails anyone), and an in-process maintenance loop drains pending rows every
`NOTIFY_DRAIN_SECONDS` with retry/backoff (`NOTIFY_MAX_ATTEMPTS`, then
terminal `failed` — inspect via `GET /v1/admin/notifications`, force a pass
with `POST /v1/admin/notifications/drain`). The same loop runs the sweeps
every `SWEEP_INTERVAL_SECONDS`, so offers, stale payments, and expired
sessions die on a clock even with zero traffic.

Mail bodies respect information asymmetry: seller emails carry the payout and
never the buyer price; buyer emails the reverse.

Set `SMTP_HOST` (plus port/credentials/`MAIL_FROM`) and mail is real — any
provider's SMTP endpoint works, and Mailpit works locally. Unset, the console
adapter logs instead of sending.

## Disputes

A buyer can dispute a **completed** job within `DISPUTE_WINDOW_DAYS` (default
7) of `completed_at` — one dispute per job; pre-completion problems still use
the cancel path. An admin arbitrates by setting a `refund_amount`
(0..buyer_price, a partial Stripe refund to the buyer) and a
`clawback_amount` (0..seller_payout, a partial transfer reversal from the
seller) **independently**; `0/0` rejects the dispute. Every resolution
appends to an append-only `adjustments` ledger — `Transaction` rows are never
edited, and `Payment.status` is never touched by a partial refund (the charge
stays `SUCCEEDED`; `REFUNDED` remains reserved for the cancel path's full
refund). `GET /v1/admin/margins/summary` reports both the gross margin and
`platform_margin_net` (gross plus clawbacks, minus refunds/chargeback
losses/fees). It also reports `fees_estimated` (the sum of each charge's
stamped `fee_estimate`) and `platform_margin_net_of_fees` (gross margin plus
adjustments, minus estimated fees) — a cash view over SUCCEEDED/REFUNDED
charges that matches what actually lands in the bank account.

Stripe chargebacks (`charge.dispute.created`/`closed`) ride the same
`POST /v1/payments/webhook` into the same `disputes` table
(`source=provider`) — the platform records the outcome and notifies admins
rather than fighting it; evidence submission stays in the Stripe dashboard (a
fork's job). A lost chargeback books a `chargeback_loss` adjustment for the
disputed amount plus a `chargeback_fee` (`CHARGEBACK_FEE_USD`, default
15.00). An admin's `resolved` status is preserved even if a chargeback closes
on the same job afterward — the loss/fee still lands in the ledger, but the
status field keeps recording the arbitration outcome; on a still-`open`
dispute, the latest provider outcome wins the status instead (one dispute
row per job, so repeat chargebacks re-annotate and re-adjudicate the same
row rather than duplicating it).

Dispute views stay asymmetric like everything else: `BuyerDisputeOut` never
carries the clawback amount, `SellerDisputeOut` never carries the refund
amount; only the admin view carries both.

## Built-in adjusters (`pricing.py`)

New adjusters register with `@register("name")`; composing/tuning is config-only.

| Name | Side | Params |
|------|------|--------|
| `surge_by_demand_ratio`  | buyer  | `max_multiplier`, `min_multiplier` |
| `time_of_day_multiplier` | both   | `multipliers: {hour: float}` |
| `new_buyer_discount`     | buyer  | `discount_pct` |
| `supply_incentive`       | seller | `max_bonus_pct` |
| `seller_tier_multiplier` | seller | `tiers: {tier: float}` |

Params are clamped at read time, so an out-of-range value is bounded, not trusted.

## Matching strategies (`matching.py`)

`cheapest_payout` (default) · `fifo` · `highest_rated` — all subject to the margin
floor and seller capacity. Register new ones with `@register_strategy("name")`.

## Out of scope / next

Trust & safety (disputes/chargebacks and partial refunds, seller→buyer
reviews, moderation — suspension, comment takedown, and counterparty
abuse reports — and notification preferences now all ship; see Disputes
above, the Buyer/Seller/Admin endpoints, and Notification preferences
below), push/SMS channels, fee-aware margin math, admin RBAC, and
OAuth/social login. See `ROADMAP.md`.
