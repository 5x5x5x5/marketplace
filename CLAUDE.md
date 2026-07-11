# marketplace — two-sided marketplace with platform margin

FastAPI + Pydantic v2 + in-memory state. Buyer-facing price and seller payout
are computed independently by pluggable pricing pipelines. Platform keeps the
spread on every matched pair.

The pivot from `auction` → `marketplace` happened on 2026-04-23. The auction
work is preserved at github.com/5x5x5x5/auction, untouched.

## Commands

- `uv sync` — install
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
- `uv run uvicorn marketplace.api:app --reload`

## Non-negotiables

- **Identity comes from the authenticated principal, never a request body.**
  Every buyer route derives `buyer_id` from a buyer token, every seller route
  derives `seller_id` from a seller token, and `/admin/*` requires an admin
  token (`auth.py`). Do not add a `buyer_id`/`seller_id` field to a request
  body — that reintroduces impersonation. Auth is pilot-grade HMAC (shared
  secret via `MARKETPLACE_SECRET`); the upgrade path is a real user store +
  provider (see `ROADMAP.md`). Details and the closed findings: `SECURITY.md`.
- **Information asymmetry is enforced by the model layer**, not by hand-curated
  dict keys. `BuyerJobView` and `SellerJobView` are separate Pydantic models;
  every buyer-facing endpoint returns the buyer view, every seller-facing
  endpoint returns the seller view. If you add a new endpoint, pick the right
  view model — never construct a dict by hand.
- **Adding an adjuster requires code; composing or tuning does not.** The
  `@register("name")` decorator in `pricing.py` is the only place new adjusters
  appear. Operators compose pipelines and tune params via admin endpoints.
- **The matching strategy is selected by name.** Adding a new strategy means
  registering it via `@register_strategy("name")` in `matching.py`. Operators
  switch between registered strategies via `PUT /admin/config/matching_strategy`.
- **Pyright strict** across `src/` and `tests/`. Do not drop to basic mode.
- **Module-level Config and Store** in `api.py` — singletons. The `reset_state()`
  helper exists for tests and only for tests.

## Subtle bits

- `default_factory=list[str]` and `default_factory=dict[str, Any]` are
  intentional: `default_factory=list` produces `list[Unknown]` under pyright
  strict, which trips `reportUnknownVariableType`.
- The seller router is included before the buyer router so `/jobs/offered`
  matches before `/jobs/{job_id}` — otherwise FastAPI tries to parse "offered"
  as a UUID and 422s.
- Quotes are single-use. POST /jobs consumes the quote (deletes it from the
  store). Re-using a quote 404s.
- `live_demand` is a proxy: count of QUOTED + MATCHED jobs for the service
  type, plus 1 for the current request. `live_supply` is the count of
  available sellers for the service type at quote time.

## Explicit non-goals (from the build spec)

Payment processing. Geo / proximity / routing. Real auth or sessions.
Persistence / DB. Real-time push or websockets. Cancellation beyond status.
Seller bidding (this is not an auction). ML-driven pricing — adjuster
architecture supports adding one as a registered function later; do not build
it now.
