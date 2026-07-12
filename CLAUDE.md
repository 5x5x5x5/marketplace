# marketplace — two-sided marketplace with platform margin

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for
local/tests). Buyer-facing price and seller payout are computed independently by
pluggable pricing pipelines; the platform keeps the spread on every matched pair.

The pivot from `auction` → `marketplace` happened on 2026-04-23. The auction
work is preserved at github.com/5x5x5x5/auction, untouched.

## Commands

- `uv sync` — install
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
- `uv run python scripts/demo.py` — full lifecycle, headless (SQLite)
- `uv run uvicorn marketplace.api:app --reload` — API (SQLite by default)
- Postgres: `docker compose up -d db && uv run alembic upgrade head` (see `.env.example`)

## Non-negotiables

- **Identity comes from the authenticated principal, never a request body.**
  Buyer routes derive `buyer_id` from a buyer token, seller routes derive
  `seller_id` from a seller token, `/v1/admin/*` requires an admin token
  (`auth.py`). Never add a `buyer_id`/`seller_id` body field — that reintroduces
  impersonation. Auth is pilot-grade HMAC with an `exp` claim (`MARKETPLACE_SECRET`);
  upgrade path is a real user store + provider. See `SECURITY.md`.
- **Information asymmetry is enforced by the model layer.** `BuyerJobView`/
  `SellerJobView`/`SellerOfferView` are separate Pydantic views; buyer endpoints
  return buyer views, seller endpoints return seller views. Never hand-build a
  dict. ORM entities (`entities.py`) never leave the API layer — map to a view.
- **Money is `Decimal`.** Quantize with `models.to_money` (2 dp, half-up),
  compare the margin floor on quantized values, serialize as JSON strings. The
  pricing pipeline stays pure `float` (ratios); quantize at the money boundary.
- **The pricing/matching core is pure.** `pricing.py` and `matching.py` operate
  on the snapshots in `config.py` (`PricingConfig`, `Candidate`), never on the DB
  session or ORM rows. `repo.py` loads those snapshots. Keep it that way.
- **Adding an adjuster/strategy requires code; composing or tuning does not.**
  `@register("name")` in `pricing.py`, `@register_strategy("name")` in
  `matching.py`. Operators tune via admin endpoints.
- **Concurrency is the DB's job.** Quote consumption, job/offer status
  transitions, and capacity checks use `session.get(..., with_for_update=True)`.
  There is no process-level lock; don't add one.
- **Pyright strict** across `src/` and `tests/`. Do not drop to basic mode.

## Subtle bits

- `default_factory=list[str]` / `default_factory=dict[str, Any]` are intentional:
  the bare `list`/`dict` trips `reportUnknownVariableType` under pyright strict.
- Tests run against SQLite by default (temp file, set in `conftest.py` before the
  app imports). `UTCDateTime` (`entities.py`) keeps datetimes tz-aware on SQLite,
  which otherwise drops tzinfo. Migrations render it as plain `DateTime` (the tz
  coercion is app-side), so `migrations/versions/*` don't import app internals.
- Quotes are single-use and swept on write (past-TTL rows deleted on the next
  quote). `POST /jobs` deletes the quote under `FOR UPDATE`.
- Offers are first-class rows. Re-match excludes any seller who already had an
  offer for that job (`repo.sellers_seen_for_job`), so decline/expiry walk the
  candidate list instead of looping. Offer expiry is a lazy sweep on reads plus
  `POST /v1/admin/jobs/sweep`.
- Seller **capacity** = accepted-but-not-completed jobs `< SellerProfile.capacity`,
  checked under a row lock on accept. Availability is not removed on accept.
- `live_demand` = PENDING + ACCEPTED jobs for the service type + 1; `live_supply`
  = available sellers at quote time.

## Explicit non-goals (roadmap, not now)

Payments (Stripe Connect destination charges = the spread), notifications,
idempotency keys, seller→buyer reviews, a background scheduler (lazy sweep +
admin trigger instead), gateway rate-limiting, and replacing pilot HMAC auth with
a real provider. Seller bidding is out (this is not an auction).
