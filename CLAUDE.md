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
  impersonation. Identity resolves through `auth_sessions` rows only — a
  session's sha256-hashed token maps to `(role, user_id)` via one indexed
  lookup. Never reintroduce token minting or a second identity verifier;
  `mint_token`/`MARKETPLACE_SECRET` are gone for good. Passwords are stored
  only as argon2 hashes via `auth.hash_password` (never plaintext, never a
  weaker hash). Email-verification and password-reset tokens are stored
  sha256-only (never the raw value) and are single-use (`used_at` set on
  consumption). See `SECURITY.md`.
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
- **Providers are only reached through `payments/port.py`.** `fake.py` and
  `stripe_provider.py` are the only two implementations; never `import stripe`
  outside `stripe_provider.py`, and never call a provider SDK directly from
  `api.py`. Selection (`payments/__init__.get_provider`) is env-driven
  (`STRIPE_SECRET_KEY`).
- **`Payment`/`Payout` record cash movement; `Transaction` stays the margin
  ledger.** Don't merge them — `Transaction.margin` is booked at completion
  regardless of payout provider status; `Payment`/`Payout` track the charge/
  transfer lifecycle against the provider.
- **`AWAITING_PAYMENT` holds a capacity slot.** A job parked there while a charge
  settles still counts against the seller's capacity — it is not free
  availability. Don't change `repo.active_job_count` to exclude it.
- **Webhook handling stays dedup-idempotent.** Every inbound event is recorded
  in `WebhookEvent` keyed on the provider's event id before it's applied; a
  replay must no-op, never re-apply.

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
- The fake payment provider (`payments/fake.py`) is a module singleton
  (`payments.fake_provider`), so app code and test-scripted state see the same
  instance; an autouse fixture resets it between tests. Don't instantiate a
  second `FakeProvider` — script the singleton instead.
- Outbound idempotency keys to the provider are derived from the job id
  (`charge:{job_id}`, `transfer:{job_id}`, `refund:{job_id}`, `acct:{seller_id}`),
  not a random value — a retry of the same operation reuses the same key on
  purpose, so it replays the original result instead of double-charging.
- The `auth` fixture (`tests/conftest.py`) white-box-inserts a `User` row with
  `id == sub` so pre-existing tests that hand a bare id (`"alice"`, `"carol"`)
  as the principal keep working without every test signing up a real account.
  That's why `User.id` is `String(128)` rather than a UUID column — it has to
  hold both real `uuid4().hex` ids (signup) and short test subs.
- `mail.use_sender` (`src/marketplace/mail.py`) is the test seam: swap in
  `RecordingEmailSender` to capture verification/reset tokens instead of
  scraping the console adapter's log output. Restore the previous sender
  (`use_sender` returns it) when done — the fixture in `conftest.py` does this
  around each test.

## Explicit non-goals (roadmap, not now)

Notifications, seller→buyer reviews, a background scheduler (lazy sweep +
admin trigger instead), gateway rate-limiting, admin RBAC (single shared admin
role for now), and OAuth/social login. Seller bidding is out (this is not an
auction). Payments now ship (Stripe Connect via `payments/port.py`, fake
provider for dev/tests) and are verified against a real Stripe test account;
disputes/chargebacks + partial refunds are still ahead. Auth now ships
(DB-backed sessions, real signup/login — see the Non-negotiables above and
`SECURITY.md`) — see `ROADMAP.md`.
