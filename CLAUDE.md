# marketplace — two-sided marketplace with platform margin

FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · Postgres (SQLite for
local/tests). Buyer-facing price and seller payout are computed independently by
pluggable pricing pipelines; the platform keeps the spread on every matched pair.

Two booking modes share one job lifecycle. **Dispatch**: a buyer asks for a
quote on a service type and the platform matches a seller. **Browse**: sellers
publish listings in a public catalog, a buyer picks one, and the job is pinned
to that seller. Server-rendered pages (`pages/`) sit over the same catalog
and session machinery as the JSON API. `rideshare`/`cleaning` in the seed and
demo data are placeholders, not a committed vertical.

How the repo is meant to be worked on (skills, reviewers, hooks) is in
`AGENTS.md` and `docs/agent-setup.md`. This file is the source of truth for
invariants.

## Commands

- `uv sync` — install
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
  — the gate (`just gate` wraps exactly this; `.claude/skills/gate/gate.sh --pg`
  adds the Postgres suite, the migration round trip and a model-drift check)
- `uv run python scripts/demo.py` — full lifecycle, headless (SQLite)
- `uv run python scripts/seed.py` — seed service types + demo users, print bearer tokens for /docs
- `uv run uvicorn marketplace.api:app --reload` — API (SQLite by default;
  `ALLOW_UNSIGNED_WEBHOOKS=true` to post fake-provider webhooks)
- Postgres: `docker compose up -d db && uv run alembic upgrade head` (see `.env.example`)
- `uv run --group mcp python -m marketplace_mcp` — ops MCP server over stdio (see `src/marketplace_mcp/README.md`)
- `bash loadtest/run_baseline.sh` — capacity harness (`uv sync --group loadtest` first; see `loadtest/README.md`)

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
  Canonical lock orders are Payment → Job and Offer → SellerProfile → Job.
  A path that already holds the Job lock and must touch Offer rows (the
  cancel paths, via `services.offers.expire_open_offers`) claims them
  `FOR UPDATE SKIP LOCKED`, never a blocking lock or a plain UPDATE. A lock
  taken after an unlocked read uses `populate_existing` and re-checks status.
- **Commits happen in `db.CommitRoute`, before the response is sent — never in
  `get_session` teardown.** Teardown (`session.close()`) runs after the
  response is already on the wire, which is finding F2 (read-your-writes race
  on every chained write); never re-add a commit there. Every router sets
  `route_class=CommitRoute` (`routers/*.py`, `auth.py`, `pages/*.py`); the import-time
  `_assert_commit_routes` invariant fails fast if a router is added without
  it (it walks routes with `fastapi.routing.iter_route_contexts`; since
  FastAPI 0.162 `app.routes` no longer lists the routes of included routers,
  so any code that loops over it directly sees almost nothing and passes
  blind) — widened in the pages-foundation phase to cover *every* route in the
  app, not just `/v1/*` (`_COMMIT_EXEMPT = {"/healthz"}` is the one deliberate
  hole; a mount like `/static`'s `StaticFiles` app isn't an `APIRoute` so it
  never trips the check). A page route that renders read-only HTML today
  still needs `CommitRoute` — a future page/form route that writes must not
  be the one that slips through. The commit itself stays on
  `run_in_threadpool` — a sync commit on the event loop would stall every
  in-flight request. Streaming responses would need a rethink of both
  `CommitRoute` and the idempotency buffer (`idempotency.py`); the app has
  none today.
- **Domain logic lives in `services/`; routers are thin; `api.py` is
  assembly only.** A service takes a `Session` plus plain arguments (ids and
  roles, never `Claims`), imports no fastapi/starlette, `api`, `routers`,
  `pages` or `auth`, and raises only `errors.DomainError` subclasses. A
  router parses, calls one service and returns; it never touches the session.
  `api._domain_error_response` is the only place a domain error becomes
  HTTP. `api.py` holds middleware, the lifespan, router mounting and the
  `_assert_commit_routes` check, and nothing else.
- **`pages/` never imports `api.py` or `routers/`.** `api.py` imports and
  mounts `pages_router`, so the reverse import is a cycle. Pages reach domain
  logic the same way the JSON routers do, by calling `services/` (and
  `catalog.py` for catalog reads), and must catch `DomainError` themselves
  to render an error page. The pure core, `repo` and `notifications` never
  import services.
- **`catalog.py` is the single source of catalog visibility/pricing truth.**
  `visible_listings_stmt`, `listing_bookable`, `seller_publicly_visible`,
  `indicative_buyer_price`, and the `catalog_listing_out`/`catalog_seller_out`
  view builders live there once; both `/v1/catalog/*` (`routers/catalog.py`) and the
  storefront pages (`pages/storefront.py`) call into them — neither re-implements the
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
  catch. The logged-in pages follow the same doctrine per audience:
  buyer pages are scanned for the payout and the margin, seller pages for
  the buyer's price, the buyer's identity and the payment client secret
  (`tests/test_pages.py::test_*_pages_never_show_*`).
- **Cookie sessions are the same `auth_sessions` rows as the API, not a
  parallel identity system.** `POST /signup` and `POST /login` (`pages/account.py`)
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
- **Every authenticated page form carries a session-bound CSRF token.**
  `pages.csrf.token_for(raw)` is `sha256("marketplace-csrf-v1\0" + raw
  session token)`: no server secret, no stored state, distinct from
  `auth_sessions.token_hash`, compared with `hmac.compare_digest`. Every
  POST in the pages package depends on `require_csrf` except
  `CSRF_EXEMPT_POSTS` (the unauthenticated forms: `/signup`, `/login`,
  `/verify`, `/password-reset/request`, `/password-reset/confirm`). A
  missing or wrong token is a 403 before the handler runs.
  `tests/test_architecture.py::test_every_page_post_requires_csrf_except_the_allowlist`
  enforces it; extending the allowlist is a security decision. Every form
  under a cookie session renders `templates/_csrf.html`.
- **A page handler that catches a `DomainError` rolls back before it
  answers** (`pages.common.redirect_after_error`). Page POSTs answer 303,
  which is under 400, so `CommitRoute` would otherwise commit whatever the
  service mutated before raising. Work a service committed itself (the
  pinned-booking 410) stays. Checked by
  `tests/test_architecture.py::test_page_handlers_roll_back_before_answering_a_domain_error`.
- **Page POSTs are post/redirect/get, and a repeat is harmless by domain
  logic, not `Idempotency-Key`.** A repeated confirm finds the job its
  quote became (`services.booking.job_for_quote`); toggles post the target
  value; status guards turn other repeats into a notice.
  `IdempotencyMiddleware` skips every pages-router POST
  (`exempt=pages.is_page_post`, wired in `api.py`): their redirects and HTML
  cannot be replayed from the JSON store. Page notices are codes
  (`?notice=<code>` selects a fixed message from `pages.common.NOTICES`); a
  service's `detail` is never put in or read from a URL.
- **The browser payment step gets only `PaymentProvider.browser_key` and the
  job's client secret.** Both reach the template only while the buyer's own
  job is `AWAITING_PAYMENT` with a `PENDING` payment. Settlement is the
  webhook, never the browser redirect. Startup refuses `STRIPE_SECRET_KEY`
  without a `pk_`-prefixed `STRIPE_PUBLISHABLE_KEY`, and refuses
  `FAKE_CHARGES_PENDING` (a demo switch) alongside Stripe.
- **`REQUIRE_VERIFIED_EMAIL` (default off) is enforced in the services.**
  It gates `create_job`, `accept_offer` and `create_listing`
  (`services.common.require_verified_email`, after `require_active`, before
  any lock or write; 403), so the API and the pages agree.
- **Token links are never consumed on GET.** `/verify` and
  `/password-reset/confirm` render a form that POSTs the token; the reset
  request redirects identically for any input.
- **Pyright strict** across `src/` and `tests/`. Do not drop to basic mode.
- **Providers are only reached through `payments/port.py`.** `fake.py` and
  `stripe_provider.py` are the only two implementations; never `import stripe`
  outside `stripe_provider.py`, and never call a provider SDK directly from
  a service or router. Selection (`payments/__init__.get_provider`) is env-driven
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
- **Webhook handling stays dedup-idempotent, and failures go to an outbox.**
  Every inbound event is recorded in `WebhookEvent` keyed on the provider's
  event id before it's applied. ANY existing row (APPLIED, FAILED or
  DEAD_LETTER) makes a redelivery a `duplicate` no-op: a FAILED event's
  recovery belongs to the retry job, never to a redelivery racing it. The
  dedup row is flushed before the apply, so two concurrent first deliveries
  meet at the unique constraint and the loser answers `duplicate`, never a
  500. A handler failure is persisted FAILED in a fresh session (attempts,
  one-line `last_error`, exponential `next_attempt_at`) and the endpoint
  answers 200 `{"status": "deferred"}`. `services.webhooks.run_webhook_retry_once`
  (maintenance loop) claims due rows with `SKIP LOCKED` and re-applies each
  under its own savepoint until APPLIED or `settings.webhook_max_attempts`;
  at the cap the row goes DEAD_LETTER and admins get the unmutable
  `WEBHOOK_DEAD_LETTER_ADMIN`. Operators list rows via
  `GET /v1/admin/webhooks` and re-queue via
  `POST /v1/admin/webhooks/{id}/retry`. `payload` is the parsed
  `PaymentEvent` fields only, never the raw body or signature.
- **Every `_apply_payment_event` branch (`services/webhooks.py`) is safe to
  apply twice and safe to apply late.** The retry job re-runs events after the job, payment or
  dispute has moved on, and after later events for the same object applied.
  A new branch needs a replay marker (a status lattice, a transition guard,
  or a durable row) plus a twice-test and a late-test in
  `tests/test_webhook_replay.py`. Money-moving markers must be permanent
  rows (for example the `CHARGEBACK_FEE` adjustment's `provider_ref`), not
  the reapable webhook ledger. `transfer_failed` for a transfer that a
  pinned dispute clawback covers in full is the platform's own reversal, not
  a failure: the payout stays PAID and nobody is paged, because a FAILED
  payout would invite an admin retry that pays the seller again.
- **The unsigned webhook is closed by default.** Without `STRIPE_SECRET_KEY`,
  `POST /v1/payments/webhook` 404s unless `ALLOW_UNSIGNED_WEBHOOKS=true`
  (dev, tests and demo only; `tests/conftest.py` and `scripts/demo.py` set
  it). Startup refuses a Stripe key without `STRIPE_WEBHOOK_SECRET`, and
  refuses `ALLOW_UNSIGNED_WEBHOOKS` alongside a Stripe key.
- **Notifications are enqueue-only inside the domain transaction.** Write them
  via `notifications.enqueue`/`enqueue_admins` in the same transaction as the
  state change — never call the mail port from an endpoint. Sends happen only
  in `notifications.drain_once` (the maintenance loop / admin drain). Payloads
  are role-safe snapshots at enqueue time: seller payloads never carry
  `buyer_price`, buyer payloads never carry `seller_payout`. The one
  exception is `auth.py`'s verification and password-reset mail, sent
  directly: those carry a raw single-use token, and the outbox would store
  it in plaintext, which the sha256-only rule forbids.
- **Access logs never carry headers, bodies, or query strings.** The access
  logger (`observability.py`) logs method/path/status/duration/request-id
  only; `path` is logged without its query component. Bearer tokens live in
  headers, reset/verification tokens live in bodies (or a hypothetical
  query string) — never add a field to the access log line without checking
  it can't carry one of those.
- **PENDING outbox rows are never reaped.** The retention sweep ages out
  `idempotency_keys`, terminal (APPLIED or DEAD_LETTER) `webhook_events` and
  terminal SENT-or-FAILED `notifications`. A `PENDING` notification is
  exempt at every age because it still needs to send, and a FAILED
  `webhook_events` row is exempt the same way: it is still owed a retry.
  Don't widen the notifications delete to include `PENDING`, or the webhook
  delete to include `FAILED`.
- **Sync DB work stays off the event loop.** `services.webhooks.process_webhook` and
  the maintenance jobs run session work via `asyncio.to_thread`, and
  `IdempotencyMiddleware` uses `run_in_threadpool`; nothing calls a session
  inline in an `async def`. A slow or blocked DB call must never stall every
  other in-flight request.
- **The connection pool is sized against the threadpool, and exhaustion sheds
  load.** `db.engine_options` builds the engine from `DB_POOL_SIZE`/
  `DB_MAX_OVERFLOW`/`DB_POOL_TIMEOUT_SECONDS` (defaults 10 + 30 = 40, one per
  default Starlette threadpool token, 5 s). A checkout timeout becomes a 503
  with `Retry-After` in `RequestIdMiddleware`, never a 500, and is never
  stored for idempotent replay. `DB_POOL=null` (NullPool) is for deployments
  behind an external pooler; SQLite ignores all of these. If you raise the
  threadpool, raise the pool with it; replicas × (size + overflow) must fit
  Postgres `max_connections`.
- **List endpoints paginate in SQL.** `services.common.paginate(session, stmt,
  limit, offset)` takes a `Select` with a total order (a unique tie-breaker, usually
  `id`). Never fetch all rows and slice in Python. Catalog pages go through
  `catalog.visible_listings_page`.
- **Read-path sweeps do only what correctness needs.** `services.maintenance.sweep` (offer expiry
  with re-match, stale-payment release) stays lazy on reads. Deletes of rows
  nothing can observe (expired quotes, sessions, email tokens, retention)
  live in `housekeeping`, run only by the maintenance loop and
  `POST /v1/admin/jobs/sweep`. The offer-expiry sweep claims offers
  `FOR UPDATE SKIP LOCKED` (Offer → Job, same order as accept and decline),
  so concurrent sweeps never re-match one offer twice.
- **The maintenance loop runs where `MAINTENANCE_ENABLED` is true** (default).
  With replicas, enable it on exactly one instance. Every job in it is safe
  to run concurrently, but N runners do N times the work.
- **Candidate loading is batched.** `repo.eligible_candidates` uses a fixed
  number of queries regardless of seller count; don't reintroduce per-seller
  queries on the quote or re-match paths.
- **`marketplace_mcp` is an HTTP-only client of `/v1/admin/*`.** It never
  imports `marketplace` internals or the DB layer, never exposes dispute
  resolution or job cancellation, and registers write tools only when
  `MARKETPLACE_MCP_ENABLE_WRITES` is set. User-authored text in tool results
  goes through `untrusted.wrap`.
- **Agent workflows are comment-and-branch only.**
  `.github/workflows/agent-*.yml` run only after a job holding no secrets
  has confirmed the actor has write access and any pull request head is in
  this repository. They use `pull_request`, never `pull_request_target`, and
  pass event text to scripts only through environment variables. Agents
  never merge, approve, or push the default branch; branch protection
  enforces that. See `docs/background-agents.md`.
- **Statically checkable rules are checked.** `tests/test_architecture.py`
  and `.semgrep/rules/` encode the rules here that a machine can verify; each
  failure names the rule and the fix. When you add a rule to this list, add
  its check if one is expressible.
- **Every real bug becomes a seeded review case.** `evals/reviewer/cases/`
  re-introduces past bugs as small mutations, and
  `tests/test_reviewer_eval_cases.py` keeps each one applying to the tree, so
  moving the code a case targets means re-anchoring the case in the same
  change (`evals/README.md`).
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
  `active=true` and `hidden=false` (`catalog.listing_bookable`) — the two flags
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
  (`services.disputes.resolve_dispute`); and pinned quote-consumption on a 410 — a
  pinned listing that's gone unavailable deletes+commits the now-dead quote
  before raising, so it can never be retried (`create_job`'s
  pinned-listing-unavailable branch, `services/booking.py`).

## Subtle bits

- `default_factory=list[str]` / `default_factory=dict[str, Any]` are intentional:
  the bare `list`/`dict` trips `reportUnknownVariableType` under pyright strict.
- Tests run against SQLite by default (temp file, set in `conftest.py` before the
  app imports). `UTCDateTime` (`entities.py`) keeps datetimes tz-aware on SQLite,
  which otherwise drops tzinfo. Migrations render it as plain `DateTime` (the tz
  coercion is app-side), so `migrations/versions/*` don't import app internals.
- Quotes are single-use; past-TTL rows are reaped by housekeeping
  (`ix_quotes_expires_at`), and the `MAX_ACTIVE_QUOTES` cap counts unexpired
  rows only. `POST /jobs` deletes the quote under `FOR UPDATE`.
- Offers are first-class rows. Re-match excludes any seller who already had an
  offer for that job (`repo.sellers_seen_for_job`), so decline/expiry walk the
  candidate list instead of looping. Offer expiry is a lazy sweep on reads plus
  `POST /v1/admin/jobs/sweep`.
- Seller **capacity** = accepted-but-not-completed jobs `< SellerProfile.capacity`,
  checked under a row lock on accept. Availability is not removed on accept.
- `live_demand` = PENDING + AWAITING_PAYMENT + ACCEPTED jobs for the service
  type + 1; `live_supply`
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
- The maintenance loop (`services.maintenance.maintenance_loop`, spawned in the lifespan)
  never runs in tests: the `client` fixture builds `TestClient(api.app)`
  without entering its context manager, so tests call
  `notifications.drain_once()` deterministically instead of waiting on ticks.
  `SMTP_HOST` is pinned empty in `conftest.py` so a developer `.env` can never
  make the suite send real mail.
- Import direction is one-way:
  `api -> routers -> services -> (notifications, repo, catalog, payments) ->
  (mail, db, entities, models)`. The maintenance loop lives in
  `services/maintenance.py` because it ticks the sweeps; putting it in
  `notifications.py` would create a cycle.

## Explicit non-goals (fork work, not template)

Deliberately left to a fork, by maintainer decision (2026-07-15):
notification digests and push/SMS channels, gateway rate-limiting (the
argon2 surfaces especially: put a limit in front of signup, login and
password reset), admin RBAC (one shared admin role here), OAuth/social
login, and automatic abuse signals/limits (report-count thresholds,
auto-suspend: fork-specific heuristics, not a generic default). Seller
bidding stays out entirely: this is a managed marketplace, not an auction.
The record of what shipped, phase by phase, is `ROADMAP.md`.
