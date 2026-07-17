# market-platform

A **browse marketplace**, forked from the generic two-sided marketplace
platform-margin template (`github.com/5x5x5x5/marketplace`) to add a public,
browsable catalog of seller listings in front of the template's own
quote-and-match flow — for a browse-family vertical with a few big sellers
per market (vertical TBD). The platform still sets the buyer-facing price
and the seller payout **independently** and keeps the spread — a *managed*
marketplace, not commission-on-seller-price — that differentiator is
unchanged by the browse work. Pricing pipelines, matching strategy, margin
floor, and adjuster parameters are all tunable at runtime via the admin API.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for local/tests)
· DB-backed session auth (argon2 password hashing via `pwdlib`). Money is
`Decimal`, serialized as JSON strings. Pyright strict across `src/` and `tests/`.

## Mechanism

1. Seller posts availability for a service type (and has a **capacity** — how many
   jobs they can hold at once).
2. Buyer requests a quote. The platform runs a buyer-side adjuster pipeline over a
   base price, and probes seller-side payouts; if the implied margin would fall
   below the configured floor *plus the estimated provider fee* the buyer price
   is bumped up (or the quote is rejected if the bump exceeds a ceiling).
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

## Browse model (fork phase 1)

Layered on top of the mechanism above, without changing it: sellers can
publish **listings** — title, description, up to 12 `https://` photo URLs,
one service type — that buyers browse before booking, and a buyer can pin a
quote to one specific listing's seller instead of taking whoever the
platform matches.

- **Listings never carry a price.** `POST/GET/PUT /v1/seller/listings`
  (seller-owned `active` toggle) and `GET/POST /v1/admin/listings/{id}
  /hide|unhide` (admin-owned takedown, independent of `active`) — a listing
  needs both `active=true` and not `hidden` to be publicly visible.
- **Public catalog** — `GET /v1/catalog/listings` (optionally filtered by
  `service_type_id`), `GET /v1/catalog/listings/{id}`, and
  `GET /v1/catalog/sellers/{id}` are all **unauthenticated**. Cards carry a
  `price_from` (the buyer pricing pipeline evaluated live, pre-floor — a
  real quote may bump it upward) and a seller summary (display name,
  rating), never a payout, capacity, email, or `payments_ready`.
- **Pinned booking** — `POST /v1/quotes` with a `listing_id` (instead of a
  bare `service_type_id`) probes and pins to *that listing's seller*: the
  quote and the resulting job both carry `pinned_seller_id`, and
  `POST /v1/jobs` offers the job to that seller only. A listing that's gone
  unavailable 409s at quote time and 410s at job-creation time (the quote is
  still consumed either way — single-use, no retry on a dead pin). A pinned
  job never re-matches to a different seller on decline, offer timeout, or
  sweep — it just expires, unlike the template's unpinned dispatch flow.
- **TTL defaults are 15/120**, not the template's original 5/2:
  `QUOTE_TTL_MINUTES=15`, `OFFER_TTL_MINUTES=120` (see `.env.example`) — a
  human browsing a catalog and picking a specific seller needs longer
  windows than the template's tighter platform-matched defaults.

## The storefront (fork phase 2)

Server-rendered HTML pages sit in front of the catalog API and auth
endpoints — no Swagger, no hand-rolled client, no SPA build step needed to
browse the catalog or hold a session in a browser. `uvicorn` already serves
these; hit `/` directly instead of `/docs` to see them.

- **`GET /`** — the catalog landing page: every publicly visible listing
  (paginated, optionally filtered by `?service_type_id=`), same visibility
  rule as `GET /v1/catalog/listings` (`catalog.visible_listings_stmt`).
- **`GET /listings/{id}`** — one listing's detail page (photos, description,
  indicative `price_from`, a link to the seller's profile). 404s (HTML, not
  JSON) if the listing doesn't exist or isn't currently visible.
- **`GET /sellers/{id}`** — a seller's public profile: rating, completed-job
  count, and their currently visible listings. 404s under the same
  visibility rule as the catalog API's `/v1/catalog/sellers/{id}`.
- **`GET/POST /signup`, `GET/POST /login`, `POST /logout`** — cookie-backed
  auth pages, restricted to `buyer`/`seller` (there is no browser login path
  to an admin session — admin stays JSON-API-only, `/v1/auth/login` with
  `role: "admin"`). A successful signup or login sets the session token as
  an `HttpOnly` cookie and redirects to `/`; logout clears it. See "Auth"
  above for the underlying session mechanics and `SECURITY.md` for the
  cookie posture.

Try it in a browser: start the API (`uv run uvicorn marketplace.api:app
--reload`), seed some listings (`uv run python scripts/seed.py`), then open
`http://localhost:8000/` — no token, no `/docs`, no `Authorize` button. Sign
up as a buyer at `/signup` and the nav switches to your display name and a
Log out link; the same `auth_sessions` row that a `POST /v1/auth/signup`
bearer client would get is now sitting in a cookie instead.

The look is deliberately light-editorial (cream background, ink text, one
accent, serif headings) rather than an "AI-generated" dark/neon default —
`src/marketplace/static/style.css`, no framework, no build step. HTMX
(`src/marketplace/static/htmx.min.js`, vendored) is wired in for phase 3;
none of today's pages issue an HTMX request yet, since none of them mutate
job/offer state.

**Phase 2 scope, and what it deliberately leaves out:** browse (catalog
landing, listing detail, seller profile) and account pages (signup/login/
logout) only. There is **no booking flow yet** — a signed-in buyer can browse
listings and see their own display name in the nav, but there's no page to
turn a listing into a quote or a job; that's still API-only, and the anonymous
listing-detail page never renders a booking form or an `/v1/quotes|/v1/jobs`
reference (`tests/test_pages.py::test_listing_detail_anonymous_no_booking_form`).
Phase 3 (see `ROADMAP.md`) adds the transact pages: the booking flow itself,
buyer/seller dashboards, an offer inbox, and the payment- and
email-token-confirmation pages, plus CSRF tokens for those money-moving forms
(this phase's Lax-cookie + POST-only posture is enough for read-mostly browse
and auth, not for money — see `SECURITY.md`).

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

## Try it in 10 minutes

The fastest tour is headless: `uv run python scripts/demo.py` runs the whole
lifecycle (quote → match → escrow → complete → dispute → refund → moderation →
stats → browse catalog + pinned booking) in-process and prints each step. To
poke at the catalog and account pages yourself, open `http://localhost:8000/`
in a browser after starting the API below — no Swagger needed for browse or
signup/login (see "The storefront" above). To drive the transact flow
(quote → job → offer → payment), which has no page yet, use Swagger:

```bash
# 1. Start the API (the admin account is seeded from these two vars)
ADMIN_EMAIL=admin@marketplace-demo.dev ADMIN_PASSWORD=try-it-admin-password \
    uv run uvicorn marketplace.api:app --reload

# 2. In another terminal: seed service types, pipelines, a buyer/seller, and a listing per type
uv run python scripts/seed.py
```

`seed.py` prints three bearer tokens and seeds one listing per service type
for the demo seller — `GET /v1/catalog/listings` is unauthenticated, so try
it with no token at all before opening Swagger. Then, in Swagger: click
**Authorize** and paste a token in (token only — no `Bearer ` prefix). Walk a
job end to end:

1. Authorize as the **buyer** → `POST /v1/quotes` with
   `{"service_type_id": "rideshare"}` (platform-matched) or
   `{"listing_id": "…"}` from a catalog card (pinned to that seller) →
   `POST /v1/jobs` with the quote id.
2. Re-Authorize as the **seller** → `GET /v1/seller/offers` →
   `POST /v1/seller/offers/{id}/accept` (the fake provider charges instantly)
   → `POST /v1/seller/jobs/{id}/complete`.
3. Re-Authorize as the **admin** → `GET /v1/admin/stats` for the operator
   snapshot and `GET /v1/admin/margins/summary` for the money — including
   `platform_margin_net_of_fees`.

Everything else in this README is reachable the same way: cancel a job and
watch the refund, file a dispute as the buyer, mute a notification kind,
suspend a user. Re-run `seed.py` any time for fresh tokens (sessions expire
after `SESSION_TTL_HOURS`, default 72). No Stripe key is needed — the
deterministic fake provider handles all money movement locally.

## Auth

Real users, not minted tokens. Signup creates a password-holding `User` row and
an opaque bearer session; every request after that carries `Authorization:
Bearer <token>`, resolved to `(role, user_id)` by one indexed lookup against
`auth_sessions`. Identity is taken from the resolved session, **never** from the
request body. Sessions are revocable (logout, password reset, and a future ban
path all just delete rows) and expire after `SESSION_TTL_HOURS` (default 72).
`GET /healthz`, `POST /v1/payments/webhook`, `/v1/catalog/*`, `/v1/auth/*`
(always unauthenticated — that's how a session is obtained in the first
place), and the whole storefront page surface (`/`, `/listings/*`,
`/sellers/*`, `/static/*`, `GET/POST /signup`, `GET/POST /login`,
`POST /logout` — see "The storefront" below) are unauthenticated; everything
else requires a resolved session. See `SECURITY.md`.

Browser pages resolve the same session a different way: `POST /signup` and
`POST /login` on the page routes (not `/v1/auth/*`) set the session token as
an `HttpOnly` cookie instead of returning it as JSON, and `_principal` reads
`Authorization: Bearer` first, falling back to that cookie — see "The
storefront" below and `SECURITY.md`'s cookie-posture section.

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

**`/v1/auth`** — `POST /signup` (returns a session) · `POST /login` ·
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

Convention: POSTs that create a durable resource (signup, quotes, jobs,
reviews, disputes, reports) return 201; POSTs that act on an existing
resource (accept, decline, complete, cancel, login, logout, verify, resolve,
suspend, reinstate, hide, unhide, etc.) return 200.

**Auth** (`/v1/auth/…`) — see the Auth section above.

**Buyer** — `POST /quotes` (`{service_type_id}` for platform-matched, or
`{listing_id}` to pin to that listing's seller — see Browse model above) ·
`POST /jobs` · `GET /jobs` · `GET /jobs/{id}` ·
`POST /jobs/{id}/cancel` · `GET /profile` (own rating aggregate) ·
`POST /jobs/{id}/review` · `GET /jobs/{id}/reviews` (own job's review(s),
no party ids) · `POST /reports` · `GET /reports` (own filed
reports; shared with seller, see Seller list)

**Seller** (`/v1/seller/…`) — `PUT|GET /profile` (own capacity) ·
`POST /payments/onboard` · `POST|DELETE /availability[/{service_type_id}]` ·
`POST /listings` · `GET /listings` (own listings, any state) ·
`PUT /listings/{id}` (title/description/photos/`active`, never a price) ·
`GET /offers` · `GET /jobs` · `POST /offers/{id}/accept` ·
`POST /offers/{id}/decline` · `POST /jobs/{id}/complete` ·
`POST /jobs/{id}/review` (rate the buyer) ·
`GET /jobs/{id}/reviews` (own job's review(s), no party ids) ·
`POST /v1/reports` ·
`GET /v1/reports` (shared with buyer, not `/seller`-prefixed — see Buyer list)

**Catalog** (`/v1/catalog/…`, unauthenticated — see Browse model above) —
`GET /listings` (optional `service_type_id` filter) ·
`GET /listings/{id}` · `GET /sellers/{id}` (aggregate stats + their
currently visible listings)

**Admin** (`/v1/admin/…`) — `GET /config` · `PUT /config/service_types/{id}` ·
`PUT /config/pipelines/{id}` · `PUT /config/margin_floor` ·
`PUT /config/matching_strategy` · `PUT /config/adjuster_params/{name}` ·
`PUT /config/fees` (pct/fixed provider-fee estimate) ·
`PUT /sellers/{id}` (tier/capacity) · `GET /buyers` (rating aggregates) ·
`GET /listings` (every listing, any state) ·
`POST /listings/{id}/hide` · `POST /listings/{id}/unhide` (admin-owned
takedown, independent of the seller's own `active` toggle) ·
`GET /transactions` · `GET /payouts` ·
`POST /payouts/{id}/retry` · `GET /notifications` ·
`POST /notifications/drain` · `GET /margins/summary` · `GET /audit` ·
`GET /stats` (operator snapshot — jobs/payments/payouts/notifications/
disputes/reports/users/quotes/retention counts, uptime) ·
`GET /jobs` · `POST /jobs/{id}/cancel` · `POST /jobs/sweep` ·
`POST /users/{id}/suspend` · `POST /users/{id}/reinstate` ·
`POST /users/{id}/reset_display_name` · `GET /reviews/{kind}` (`buyer`|`seller`) ·
`POST /reviews/{kind}/{id}/hide` · `POST /reviews/{kind}/{id}/unhide` ·
`GET /reports` · `POST /reports/{id}/resolve`

**Payments** — `POST /payments/webhook` (provider event sink, unauthenticated,
signature-verified)

**Disputes** — Buyer: `POST /jobs/{id}/dispute` · `GET /jobs/{id}/dispute`.
Seller: `GET /v1/seller/jobs/{id}/dispute`. Admin: `GET /disputes` ·
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

Fourteen lifecycle events email the right party: the seller's new-offer alert
(the one that makes short offer TTLs livable — see `OFFER_TTL_MINUTES` in
the Browse model section above; this fork's default is 120, not the
template's original 2), the buyer's
accepted/payment-due, completed, expired, and refund notices, a cancel notice
to a committed seller, dispute outcomes to both sides, and report/payout-failure
alerts to every admin. Recipients tune this per kind via
`GET/PUT /v1/notification-preferences` (role-scoped mutes) — except four
money-critical kinds (refunds, dispute outcomes, payout failures) that can
never be muted by any path; see `SECURITY.md`.

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

## Observability & ops

Every request gets a request id: an inbound `X-Request-ID` header is honored
(validated — hostile or oversized values are replaced), otherwise one is
generated; it's echoed on the response and threaded through every log line
for that request, access and application both. Logs are JSON by default
(`LOG_FORMAT=plain` switches to a one-line human format for local reading);
the access log never carries headers, bodies, or query strings. A single
request-boundary middleware turns any unhandled exception into a clean
`{"detail": "internal error", "request_id": …}` 500 — the traceback goes to
the log, never the response.

`GET /v1/admin/stats` is the one-call operator snapshot: job/payment/payout/
notification/dispute/report/user/quote counts, retention-table row counts,
and process uptime — no need to piece it together from several endpoints.

Three tables age out on the maintenance loop's own clock rather than
growing forever: `idempotency_keys` (`RETENTION_IDEMPOTENCY_DAYS`, default
7), `webhook_events` (`RETENTION_WEBHOOKS_DAYS`, default 30), and terminal
SENT/FAILED `notifications` (`RETENTION_NOTIFICATIONS_DAYS`, default 30).
PENDING notification rows are never swept, regardless of age — they still
need to send.

API hardening: `MAX_BODY_BYTES` (default 1 MiB, 1_048_576) caps request
bodies with a 413, checked against a declared `Content-Length` up front and
counted on chunked bodies; `TRUSTED_HOSTS` and `CORS_ORIGINS` back
`TrustedHostMiddleware`/`CORSMiddleware` (both open — `["*"]`/`[]` — by
default for dev, narrow them for production). Admin list endpoints take
`limit`/`offset` query params (capped) for pagination.

Both `TRUSTED_HOSTS` and `CORS_ORIGINS` are `pydantic-settings` list fields,
so the env var must be a **JSON array**, not a bare comma-separated string —
`TRUSTED_HOSTS=api.example.com` fails at boot; the working form is:

```bash
TRUSTED_HOSTS='["api.example.com"]'
CORS_ORIGINS='["https://app.example.com"]'
```

## Out of scope (fork work)

The template is feature-complete — the full record is in `ROADMAP.md`.
What's deliberately left to a fork: push/SMS notification channels,
notification digests, gateway rate-limiting, admin RBAC (one shared admin
role here), OAuth/social login, automatic abuse signals/limits
(report-count thresholds, auto-suspend — fork-specific heuristics, not a
generic default), and seller bidding (this is a managed marketplace, not
an auction).
