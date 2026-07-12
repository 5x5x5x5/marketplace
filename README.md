# marketplace

A generic **two-sided marketplace** template. The platform sets the buyer-facing
price and the seller payout **independently** and keeps the spread — a *managed*
marketplace, not commission-on-seller-price. Pricing pipelines, matching
strategy, margin floor, and adjuster parameters are all tunable at runtime via
the admin API. Fork it and specialize it per market.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for local/tests)
· pilot-grade HMAC auth. Money is `Decimal`, serialized as JSON strings. Pyright
strict across `src/` and `tests/`.

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

Every request carries a bearer token asserting a role (`buyer`/`seller`/`admin`),
a subject id, and an expiry. Identity is taken from the token, **never** from the
request body. Pilot-grade HMAC (`src/marketplace/auth.py`); set `MARKETPLACE_SECRET`
outside local dev. `GET /healthz` is unauthenticated. See `SECURITY.md`.

```python
from marketplace.auth import mint_token
headers = {"Authorization": f"Bearer {mint_token('buyer', 'alice')}"}
```

## Endpoints (all under `/v1`)

**Buyer** — `POST /quotes` · `POST /jobs` · `GET /jobs` · `GET /jobs/{id}` ·
`POST /jobs/{id}/cancel` · `POST /jobs/{id}/review`

**Seller** (`/v1/seller/…`) — `PUT|GET /profile` (own capacity) ·
`POST|DELETE /availability[/{service_type_id}]` · `GET /offers` · `GET /jobs` ·
`POST /offers/{id}/accept` · `POST /offers/{id}/decline` · `POST /jobs/{id}/complete`

**Admin** (`/v1/admin/…`) — `GET /config` · `PUT /config/service_types/{id}` ·
`PUT /config/pipelines/{id}` · `PUT /config/margin_floor` ·
`PUT /config/matching_strategy` · `PUT /config/adjuster_params/{name}` ·
`PUT /sellers/{id}` (tier/capacity) · `GET /transactions` · `GET /margins/summary` ·
`GET /audit` · `GET /jobs` · `POST /jobs/{id}/cancel` · `POST /jobs/sweep`

## Job & offer state machine

- **Job**: `pending → accepted → completed`; plus `expired` (no seller took it) and
  `cancelled`.
- **Offer**: `offered → accepted | declined | expired`. Expiry/decline re-match to
  the next eligible seller (lazy sweep on reads + `POST /admin/jobs/sweep`).

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

Payments (Stripe Connect destination charges map to the spread), notifications,
idempotency keys, seller→buyer reviews, a background scheduler, and replacing the
pilot HMAC auth with a real provider. See `ROADMAP.md`.
