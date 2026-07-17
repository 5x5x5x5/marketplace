# market-platform — browse marketplace on the platform-margin template

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for
local/tests). Buyer-facing price and seller payout are computed independently by
pluggable pricing pipelines; the platform keeps the spread on every matched pair.

This is a **private fork** (`github.com/5x5x5x5/market-platform`, remote
`origin`) of the public feature-complete template
(`github.com/5x5x5x5/marketplace`, remote `upstream`). The fork is
specializing the template into a **browse marketplace**: a public catalog of
seller listings that buyers browse before booking, for a browse-family
vertical with a few big sellers per market (the specific vertical is TBD —
`rideshare`/`cleaning` in the seed/demo data are placeholders, not the
committed vertical). Phase 1 (browse core: listings, public catalog, pinned
chosen-seller booking — branch `browse-core`) and Phase 2 (pages foundation:
server-rendered catalog/listing/seller pages plus cookie-backed signup/login,
this branch, `pages-foundation`) are both done; see `ROADMAP.md`'s fork
section for phase 3. The template's own dispatch mode (quote →
platform-matched offer, no listing) still works underneath and is exercised
by demo acts 1-8; browse/pinned booking is additive, not a replacement.

The pivot from `auction` → `marketplace` happened on 2026-04-23, upstream. The
auction work is preserved at github.com/5x5x5x5/auction, untouched.

## Commands

- `uv sync` — install
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
- `uv run python scripts/demo.py` — full lifecycle, headless (SQLite)
- `uv run python scripts/seed.py` — seed service types + demo users, print bearer tokens for /docs
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
- **The margin floor check is net-of-fees.** Both enforcement sites compare
  against `matching.required_spread(buyer_price, margin_floor, fees)` —
  never the gross spread. `Payment.fee_estimate` is a stamp-time snapshot
  (computed from the platform fee config once, at charge time); it is never
  recomputed later, even if the fee config subsequently changes.
- **The pricing/matching core is pure.** `pricing.py` and `matching.py` operate
  on the snapshots in `config.py` (`PricingConfig`, `Candidate`), never on the DB
  session or ORM rows. `repo.py` loads those snapshots. Keep it that way.
- **Adding an adjuster/strategy requires code; composing or tuning does not.**
  `@register("name")` in `pricing.py`, `@register_strategy("name")` in
  `matching.py`. Operators tune via admin endpoints.
- **Concurrency is the DB's job.** Quote consumption, job/offer status
  transitions, and capacity checks use `session.get(..., with_for_update=True)`.
  There is no process-level lock; don't add one.
- **Commits happen in `db.CommitRoute`, before the response is sent — never in
  `get_session` teardown.** Teardown (`session.close()`) runs after the
  response is already on the wire, which is finding F2 (read-your-writes race
  on every chained write); never re-add a commit there. Every router sets
  `route_class=CommitRoute` (`api.py`, `auth.py`, `pages.py`); the import-time
  `_assert_commit_routes` invariant fails fast if a router is added without
  it — widened in the pages-foundation phase to cover *every* route in the
  app, not just `/v1/*` (`_COMMIT_EXEMPT = {"/healthz"}` is the one deliberate
  hole; a mount like `/static`'s `StaticFiles` app isn't an `APIRoute` so it
  never trips the check). A page route that renders read-only HTML today
  still needs `CommitRoute` — a future page/form route that writes must not
  be the one that slips through. The commit itself stays on
  `run_in_threadpool` — a sync commit on the event loop would stall every
  in-flight request. Streaming responses would need a rethink of both
  `CommitRoute` and the idempotency buffer (`idempotency.py`); the app has
  none today.
- **`pages.py` never imports `api.py`.** Import direction is one-way:
  `pages -> (db, auth, catalog, entities, models)`. `api.py` imports and
  mounts `pages_router` (`app.include_router(pages_router)`); the reverse
  import would be a cycle. If a page ever needs API-layer logic, move the
  shared piece into a module both can import (as `catalog.py` did for
  catalog reads) — never import `api` from `pages`.
- **`catalog.py` is the single source of catalog visibility/pricing truth.**
  `visible_listings_stmt`, `listing_bookable`, `seller_publicly_visible`,
  `indicative_buyer_price`, and the `catalog_listing_out`/`catalog_seller_out`
  view builders live there once; both `/v1/catalog/*` (`api.py`) and the
  storefront pages (`pages.py`) call into them — neither re-implements the
  join, the filter, or the pricing call. This branch caught two double-
  expression regressions from an earlier draft that hand-rolled the same
  visibility check in two places and let them drift; don't reintroduce a
  second copy for a new consumer (an admin page, a feed, a search index) —
  extend `catalog.py` instead.
- **Jinja autoescape is a security boundary, not a formatting default.**
  Seller-supplied content (listing title/description, display name) renders
  on pages nobody has to log in to see. Autoescape is on by default for
  `.html` templates (`Jinja2Templates`) — never mark a template `|safe`, never
  hand-build HTML strings for user data, and never disable autoescape on a
  template that touches `Listing`/`User` fields. Covered by
  `tests/test_pages.py::test_xss_listing_title_escaped*` (element-content
  sink) and `::test_xss_listing_title_escaped_detail_and_seller`'s `<img
  alt=…>` check (attribute-context sink — a different escaping rule than
  element content, so it's tested separately).
- **Public page source obeys the same asymmetry doctrine as the JSON catalog
  views.** The rendered HTML of `/`, `/listings/{id}`, and `/sellers/{id}`
  must never contain `seller_payout`, `margin`, `capacity`,
  `payments_ready`, or a seller/buyer email — same forbidden set as
  `CatalogListingOut`/`CatalogSellerOut`, because the pages are built from
  those same view objects (see the `catalog.py` bullet above). Enforced by a
  literal string scan over the rendered page,
  `tests/test_pages.py::test_page_source_asymmetry*` — a future page adding a
  new field to the template context is exactly the case that scan exists to
  catch.
- **Cookie sessions are the same `auth_sessions` rows as the API, not a
  parallel identity system.** `POST /signup` and `POST /login` (`pages.py`)
  call the identical `auth.signup`/`auth.login` the JSON API uses, then set
  the *raw* session token as an `HttpOnly`+`SameSite=Lax` cookie (`Secure`
  gated on `settings.cookie_secure`, off by default for local HTTP, on
  behind TLS). `auth._resolve_principal` is header-first, cookie-fallback —
  an `Authorization: Bearer` header always wins over the `session_token`
  cookie when both are present — so API clients and browser pages share one
  resolver with no special-casing per caller. Logout and password-reset
  revoke the row, which revokes the cookie session identically to a bearer
  session, because it *is* the same row; there is no separate cookie-session
  TTL or revocation path to keep in sync. See `SECURITY.md`'s cookie-posture
  section.
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
- **`adjustments` is append-only and `Transaction` rows are immutable.** A
  dispute resolution never edits a booked row — it appends `refund`/
  `clawback`/`chargeback_loss`/`chargeback_fee` rows and reports both gross
  and net-of-adjustments margin. `Payment.status` is never touched by a
  partial refund (the charge stays `SUCCEEDED`; `REFUNDED` stays reserved for
  the cancel path's full refund). Dispute views are role-scoped exactly like
  job views — `BuyerDisputeOut`/`SellerDisputeOut`/`AdminDisputeOut` split the
  same way `BuyerJobView`/`SellerJobView` do.
- **`AWAITING_PAYMENT` holds a capacity slot.** A job parked there while a charge
  settles still counts against the seller's capacity — it is not free
  availability. Don't change `repo.active_job_count` to exclude it.
- **Webhook handling stays dedup-idempotent.** Every inbound event is recorded
  in `WebhookEvent` keyed on the provider's event id before it's applied; a
  replay must no-op, never re-apply.
- **Notifications are enqueue-only inside the domain transaction.** Write them
  via `notifications.enqueue`/`enqueue_admins` in the same transaction as the
  state change — never call the mail port from an endpoint. Sends happen only
  in `notifications.drain_once` (the maintenance loop / admin drain). Payloads
  are role-safe snapshots at enqueue time: seller payloads never carry
  `buyer_price`, buyer payloads never carry `seller_payout`.
- **Access logs never carry headers, bodies, or query strings.** The access
  logger (`observability.py`) logs method/path/status/duration/request-id
  only; `path` is logged without its query component. Bearer tokens live in
  headers, reset/verification tokens live in bodies (or a hypothetical
  query string) — never add a field to the access log line without checking
  it can't carry one of those.
- **PENDING outbox rows are never reaped.** The retention sweep ages out
  `idempotency_keys`/`webhook_events`/terminal SENT-or-FAILED
  `notifications`, but a `PENDING` notification is exempt at every age —
  it still needs to send. Don't widen the notifications retention delete
  to include `PENDING`.
- **Webhook DB work stays off the event loop.** `_process_webhook` runs
  synchronous session work via `asyncio.to_thread` (`api.py`), not inline
  in the async handler — a slow/blocked DB call must never stall the event
  loop for every other in-flight request. Keep new webhook-handling code in
  that thread-offloaded function, not back on the coroutine.
- **Listings never carry prices.** Platform-set, independently-computed
  buyer price and seller payout are the differentiator (see the top of this
  file) — a `Listing` (`entities.py`) is title/description/photos/service
  type only. `POST/PUT /v1/seller/listings` never accept a price field, and
  never will; a seller "pricing" their own listing would collapse the whole
  spread model back into seller-set-price-plus-commission.
- **Public catalog responses never carry payouts, capacity, emails, or
  `payments_ready`.** `CatalogListingOut`/`CatalogSellerOut`/
  `CatalogSellerProfileOut` (`models.py`) are a third view family alongside
  buyer/seller/admin — asymmetry doctrine extends to unauthenticated browse.
  `price_from` is the buyer pipeline evaluated pre-floor, never the seller's
  payout or the margin.
- **`Listing.hidden` is admin-owned; `Listing.active` is seller-owned.**
  Never let a seller-facing endpoint touch `hidden` (that's takedown,
  `/v1/admin/listings/{id}/hide|unhide`) and never let an admin endpoint
  touch `active` (that's the seller's own on/off toggle,
  `PUT /v1/seller/listings/{id}`). Catalog visibility requires both
  `active=true` and `hidden=false` (`api._listing_bookable`) — the two flags
  are independent, not a shared boolean.
- **Pinned jobs never re-match to another seller.** A quote created with
  `listing_id` stamps `pinned_seller_id` on both the quote and the resulting
  job; the buyer picked that seller, so decline/expiry/sweep on a pinned job
  end it (expire it) rather than walking the candidate list to a rival
  seller. Only the template's original unpinned dispatch path (no
  `listing_id`) re-matches. Don't fold pinned jobs into
  `repo.sellers_seen_for_job`'s re-match loop.
- **A handler returning a mutated/created ORM object must `session.flush()`
  before returning it.** `CommitRoute` serializes the response model from
  the returned object *before* the commit runs (so a validation error still
  400s instead of committing); an unflushed object can carry stale/absent
  server-generated ids or timestamps into that serialization. Every listing
  mutation handler (`create_listing`, `update_listing`, admin
  hide/unhide) flushes before `return`.
- **A handler that must persist state ahead of a possible error response has
  to commit mid-handler, with a comment naming the mechanism.** `CommitRoute`
  only commits 2xx responses — it never persists anything for a raised
  `HTTPException` (4xx or 5xx). Two deliberate precedents: dispute
  resolution pins `refund_amount`/`clawback_amount` and commits *before*
  calling the payment provider, so a subsequent provider failure (502) still
  leaves the pinned amounts durable for a retry to converge on
  (`resolve_dispute`, `api.py`); and pinned quote-consumption on a 410 — a
  pinned listing that's gone unavailable deletes+commits the now-dead quote
  before raising, so it can never be retried (`create_job`'s
  pinned-listing-unavailable branch, `api.py`).

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
- Dispute resolution's provider keys — `refund:{job_id}:dispute` and
  `reversal:{job_id}:dispute` — are deliberately distinct from the cancel
  path's `refund:{job_id}` key, so a post-completion partial refund can never
  replay (or be replayed by) a full cancel refund. One dispute per job keeps
  each key unique per operation.
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
- The maintenance loop (`api._maintenance_loop`, spawned in the lifespan)
  never runs in tests: the `client` fixture builds `TestClient(api.app)`
  without entering its context manager, so tests call
  `notifications.drain_once()` deterministically instead of waiting on ticks.
  `SMTP_HOST` is pinned empty in `conftest.py` so a developer `.env` can never
  make the suite send real mail.
- Import direction for notifications is one-way:
  `api -> notifications -> (mail, db, entities, models)`. The loop lives in
  `api.py` because it ticks `_sweep`; putting it in `notifications.py` would
  create a cycle.

## Explicit non-goals (fork work, not template)

The template is feature-complete (10 migrations; every phase — payments,
real-user auth, notifications + scheduler, disputes/chargebacks,
seller→buyer reviews, moderation, notification preferences, fee-aware
margin, observability & ops — ships; the full record lives in `ROADMAP.md`
and the invariants above). This fork's browse-core phase adds an 11th
(listings; see `ROADMAP.md`'s fork section) — an expected, ongoing count as
fork phases land, not a template regression. Deliberately left to a fork, by
maintainer
decision (2026-07-15): notification digests and push/SMS channels, gateway
rate-limiting, admin RBAC (one shared admin role here), OAuth/social login,
and automatic abuse signals/limits (report-count thresholds, auto-suspend —
fork-specific heuristics, not a generic default). Seller bidding stays out
entirely: this is a managed marketplace, not an auction.
