# Booking pages — design

**Date:** 2026-11-10 · **Status:** approved · **Branch:** `wt/pages`

## Goal

The storefront stops at browsing: the listing page says "Booking arrives in
phase 3", and with Stripe configured there is no card-entry step anywhere,
because a charge is created without a payment method and waits for
client-side confirmation. This phase adds the server-rendered pages a buyer
needs to quote, book, pay, follow and cancel a booking, and the seller
dashboard a seller needs to accept, decline and complete work and manage
listings, availability and payments onboarding. It adds CSRF tokens to every
authenticated page form (deferred to this phase by `SECURITY.md`), an
opt-in email-verification gate enforced in the services, and the verify and
password-reset pages the mail links already point at.

## Decisions

1. **Pages call services, nothing else.** Every write goes through the
   existing `services/` function the JSON router calls (`booking.create_quote`,
   `booking.create_job`, `jobs.cancel_job`, `jobs.accept_offer`,
   `jobs.decline_offer`, `jobs.complete_job`, `listings.*`, `profiles.*`,
   `payouts.onboard_payments`) and the existing `auth` functions for
   verify/reset. Where a page needs a read no service offers, a small read
   is added to the service module that owns the table (Decision 13), never
   written in the page. The money paths (accept, complete, cancel) are
   unchanged.

2. **`pages.py` becomes a package**, `pages/`, with `pages_router` still
   importable as `marketplace.pages.pages_router`:
   `common.py` (templates, rendering, page errors, principal dependencies,
   notices), `csrf.py`, `storefront.py` (landing, listing, seller profile),
   `account.py` (signup, login, logout, verify, reset), `buyer.py`,
   `seller.py`. Each module builds its own `APIRouter(route_class=CommitRoute,
   tags=["pages"])`; `__init__` includes them into `pages_router`
   (`include_router` keeps each route's class). The import direction rule is
   unchanged: the package never imports `api` or `routers`.

3. **A domain error on a page POST rolls back, then redirects.** Page
   handlers catch `DomainError`, call `session.rollback()`, and answer 303 to
   the page the user came from with a `notice=<code>` query parameter.
   The rollback is load-bearing: a 303 is under 400, so `CommitRoute` would
   otherwise commit whatever the service had mutated before it raised (for
   example `accept_offer` marks an expired offer EXPIRED and then raises; the
   JSON API discards that, and the page must too). The pinned-booking 410
   path already committed its quote deletion itself, so the rollback leaves
   it in place, exactly as on the API. All of this lives in one helper,
   `common.redirect_after_error`.

4. **Notices are codes, never reflected text.** The `notice` query parameter
   is looked up in a fixed server-side table of messages; an unknown code
   renders nothing. A service's `detail` string is never put in a URL or
   echoed from one, so a crafted link cannot inject copy into the page.

5. **Post/redirect/get everywhere, and double submits are harmless.**
   - *Quote* (`POST /listings/{id}/quote`): a second submit creates a second
     quote; quotes are cheap, single-use and expire. Harmless.
   - *Book* (`POST /quotes/{id}/book`): the quote is consumed under
     `FOR UPDATE`. A second submit finds no quote; the page then looks up the
     job this buyer created from that quote (`Job.quote_id`) and redirects to
     it. A concurrent second submit blocks on the quote row lock, sees it
     deleted after the first commits, and lands on the same booking. If no
     job exists (the pinned listing went away and the quote was consumed by
     the 410 path), the page shows "no longer available" with a link back.
     `GET /quotes/{id}` after booking redirects to the booking the same way.
   - *Cancel, accept, decline, complete*: the services' status guards make a
     repeat a 409; the page redirects back with a neutral notice and the page
     shows the current state.
   - *Listing active toggle and availability*: the form sends the target
     value (`active=true|false`, `on=true|false`), not "flip", so a repeat is
     a no-op. Turning availability off when it is already off is treated as
     success.

6. **CSRF token: a domain-separated hash of the session token.**
   `csrf_token = sha256("marketplace-csrf-v1\x00" + raw_session_token)`, hex.
   - *Bound to the session:* it is derived from the raw token in the
     `session_token` cookie, so it changes with every login and dies with the
     session (logout and password reset delete the row). There is no stored
     state and no second session mechanism.
   - *No server-side secret:* the raw session token is itself a 256-bit
     secret only the browser holds (the cookie is `HttpOnly`); nothing new is
     configured, and `CLAUDE.md`'s ban on a signing secret stands.
   - *Distinct from the stored hash:* `auth_sessions.token_hash` is
     `sha256(raw)`. The CSRF value hashes a fixed prefix plus the raw token,
     so a database reader holding `token_hash` cannot compute it, and a page
     reader holding the CSRF value learns nothing about `raw` or
     `token_hash` (preimage resistance). Prefix, not suffix, so the value is
     not a length-extension of anything stored.
   - *Checked in constant time* with `hmac.compare_digest`.
   - *Fails closed:* a missing cookie, a missing field or a mismatch is a
     403 page, before the handler runs. It is a FastAPI dependency
     (`csrf.require_csrf`) on every page POST except a named allowlist of
     unauthenticated forms: `/signup`, `/login`, `/verify`,
     `/password-reset/request`, `/password-reset/confirm`. `/logout` is
     authenticated and requires the token.
   - *Rejected alternatives:* a signed double-submit cookie (needs a server
     secret); a token stored in a new table (a second piece of session
     state to keep in sync with `auth_sessions`); `SameSite=Lax` alone (a
     same-site subdomain, or a browser that does not enforce Lax, rides the
     cookie on a money-moving POST).
   - *Bearer requests:* a request that authenticates with an
     `Authorization` header but has no cookie fails the check. Page forms are
     for browsers; the JSON API is the bearer surface, and a browser never
     attaches that header cross-site.
   - *Residual risk accepted:* login CSRF (an attacker logs the victim into
     the attacker's account) is not addressed: pre-session forms have no
     session to bind a token to, and the fix would be a pre-session cookie.
     `SameSite=Lax` and the page login's refusal of the admin role remain.

7. **The idempotency middleware skips every page POST.** Page POSTs answer
   with 303 redirects (with `Location`, sometimes `Set-Cookie`) and HTML
   errors; the replay store keeps only status and body and replays them as
   JSON, so a captured page response could only replay broken. Double-submit
   safety on pages comes from the domain (Decision 5), not `Idempotency-Key`,
   which browsers never send. The rule is router membership, not a path
   prefix: `api.py` hands the middleware `pages.is_page_post`, which matches
   a path against the POST routes of `pages_router`. This replaces the
   hand-listed `_PAGE_AUTH_PATHS`; a new page POST is exempt because it is a
   page, and nothing outside `pages_router` is.

8. **Payment step.** When the buyer's job is `AWAITING_PAYMENT` and its
   payment is `PENDING` with a client secret, the booking page offers
   payment.
   - With Stripe: the page loads `https://js.stripe.com/endive/stripe.js`
     (Stripe's current versioned path; Stripe requires loading it from
     js.stripe.com, never self-hosted) and `/static/payment.js`, which reads
     the publishable key, the client secret and the return URL from `data-`
     attributes, mounts the Payment Element (`stripe.elements({clientSecret})`,
     `elements.create("payment")`) and calls
     `stripe.confirmPayment({elements, confirmParams: {return_url}})`.
     An immediate error is shown in the form; otherwise Stripe redirects to
     the booking page. Settlement is the existing `payment_intent.succeeded`
     webhook; the page never trusts the redirect's query parameters.
     `payment.js` strips `payment_intent_client_secret` and friends from the
     address bar with `history.replaceState`, and page responses carry
     `Referrer-Policy: same-origin` so neither the secret nor a reset token
     leaves in a `Referer`.
   - With the fake provider: nothing is mounted and no Stripe script is
     loaded; the page explains the payment is pending confirmation and the
     status poll picks up the dev webhook.
   - The browser key reaches the template through the payment port:
     `PaymentProvider.browser_key` (`None` on the fake, the publishable key
     on Stripe). `pages` never imports `stripe` and never reads Stripe
     settings; it passes `browser_key` and the client secret to the template,
     only when a payment is pending, only to the job's buyer (the job read is
     ownership-checked; `BuyerJobView.client_secret` is already set only
     while `AWAITING_PAYMENT`).
   - **The live Payment Element flow is unverified**: there is no Stripe
     account on the build machine. Tests cover what reaches the template and
     when, the settings validator, and that no Stripe script loads in fake
     mode.

9. **Status follows itself.** The booking page polls
   `GET /bookings/{id}/status?shown=<status>` every 4 s with htmx, only
   while the job is `pending` or `awaiting_payment`. The fragment re-renders
   the status block; when the status differs from `shown` it answers with
   `HX-Refresh: true` so the whole page (payment form, cancel button)
   re-renders from server state. Without JavaScript, a "Refresh status" link
   does the same. The poll uses the same `jobs.get_job_buyer` read as the
   page (which runs the lazy sweep, as the JSON read does).

10. **Email-verification gate.** `require_verified_email: bool = False`.
    When on, `booking.create_job`, `jobs.accept_offer` and
    `listings.create_listing` call `services.common.require_verified_email`
    after the suspension check and before any lock or write, raising
    `errors.EmailNotVerifiedError` (a `ForbiddenError`, 403, detail
    `verify your email address first`). A missing `User` row fails closed.
    The JSON API and the pages behave the same because the check is in the
    services. Quoting stays open (it moves nothing). Pages render a notice
    with a "resend verification email" button (`POST /verify/resend`,
    authenticated, CSRF-protected, `auth.resend_verification`).

11. **Verify and reset pages.** `GET /verify?token=…` and
    `GET /password-reset/confirm?token=…` never consume the token on GET (mail
    scanners prefetch links); they render a form that POSTs it.
    `POST /verify` calls `auth.verify_email`; success redirects to
    `/verify/done`, failure is a 400 page with one generic message.
    `POST /password-reset/request` calls `auth.request_password_reset` and
    always redirects to `/password-reset/sent`, whatever the input (malformed
    email included), so the page reveals nothing about which addresses
    exist. `POST /password-reset/confirm` calls `auth.confirm_password_reset`;
    success clears the cookie (every session was just revoked) and redirects
    to `/login?notice=password_reset`; a bad token is a generic 400 page; a
    too-short password re-renders the form. Tokens travel in the query
    string (never logged: the access log drops the query) and in POST bodies
    (never logged), and the token pages send `Cache-Control: no-store`.

12. **Seller onboarding from the dashboard.** `POST /seller/payments/onboard`
    calls `payouts.onboard_payments`. If the account is ready at once (the
    fake provider), it redirects to the dashboard with a notice; otherwise it
    redirects to the provider's hosted onboarding URL.
    `ONBOARDING_RETURN_URL` defaults to `BASE_URL/onboarded`, which had no
    handler; `GET /onboarded` now redirects to `/seller`.

13. **New service reads and view fields** (all small, all in the module that
    owns the table):
    - `booking.get_quote(session, buyer_id, quote_id) -> Quote` (404 if
      missing or not this buyer's),
      `booking.job_for_quote(session, buyer_id, quote_id) -> Job | None`.
    - `listings.get_own_listing(session, seller_id, listing_id) -> Listing`.
    - `profiles.availability_by_service_type(session, seller_id) ->
      list[tuple[str, bool]]` (every service type, and whether this seller
      is available for it).
    - `catalog.public_seller(session, seller_id) -> CatalogSellerOut | None`
      (the public summary when the seller is publicly visible).
    - `BuyerJobView.pinned_seller_id` (the seller the buyer chose; their own
      choice, public on the listing) so the booking page can name the seller
      before acceptance. `SellerProfileOut.payments_ready` (the seller's own
      onboarding state, on the seller's own view; still absent from every
      public view).

14. **Page identity.** `common.page_buyer` / `common.page_seller`
    dependencies resolve claims with the shared `auth.optional_claims`
    resolver. No principal: a 303 to `/login` (the handler never runs, so
    nothing commits). Wrong role (including an admin bearer or cookie): a 403
    page. No form carries a `buyer_id`/`seller_id`, hidden or otherwise.
    These are raised as `common.PageInterrupt` and rendered by one exception
    handler that `api.py` registers.

15. **Fake provider: an opt-in pending mode for demos.** `FAKE_CHARGES_PENDING`
    (default false) makes the fake provider leave every charge `PENDING` with
    a client secret, so a browser demo shows the payment step and the dev
    webhook settles it. Ignored with Stripe; refused alongside
    `STRIPE_SECRET_KEY` by the settings validator so it can never be
    mistaken for a production switch.

## Data model

No migration. `Job.quote_id`, `Job.pinned_seller_id`, `Payment.client_secret`
and `User.email_verified` already exist.

## Provider calls and idempotency keys

Unchanged: accept charges with `charge:{job_id}`, complete transfers with
`transfer:{job_id}`, cancel refunds with `refund:{job_id}`, onboarding uses
`acct:{seller_id}`. The port gains one attribute, `browser_key: str | None`.
`StripeProvider(secret, webhook_secret, publishable_key="")`;
`get_provider` passes `settings.stripe_publishable_key`.

## State transitions and locking

No new transitions. Every status change goes through the existing service,
so lock order (Offer → SellerProfile → Job on accept/decline, Payment → Job
on cancel) and the `populate_existing` re-reads are inherited unchanged. The
new reads take no locks. Page handlers never commit mid-handler; the only
mid-handler commit on these paths is the existing one in `create_job`'s
pinned-unavailable branch.

## Endpoints and views

| Method, path | Caller | Behaviour |
|---|---|---|
| GET `/listings/{id}` | anyone | unchanged for anonymous (no form); buyer sees "Get a price" form; seller sees a note |
| POST `/listings/{id}/quote` | buyer, CSRF | `create_quote(listing_id)` → 303 `/quotes/{qid}`; error → 303 back with notice |
| GET `/quotes/{id}` | buyer | price, expiry, seller, confirm form; consumed → 303 to its booking; expired → 410 page |
| POST `/quotes/{id}/book` | buyer, CSRF | `create_job` → 303 `/bookings/{jid}`; Decision 5 for repeats |
| GET `/bookings` | buyer | own jobs, newest first, paginated in SQL |
| GET `/bookings/{id}` | buyer (owner) | status, payment step, cancel; 404 page otherwise |
| GET `/bookings/{id}/status` | buyer (owner) | htmx fragment; `HX-Refresh` on change |
| POST `/bookings/{id}/cancel` | buyer, CSRF | `cancel_job` → 303 back |
| GET `/seller` | seller | dashboard: payments, availability, offers, jobs, listings |
| POST `/seller/offers/{id}/accept` | seller, CSRF | `accept_offer` → 303 `/seller` |
| POST `/seller/offers/{id}/decline` | seller, CSRF | `decline_offer` → 303 `/seller` |
| POST `/seller/jobs/{id}/complete` | seller, CSRF | `complete_job` → 303 `/seller` |
| GET `/seller/listings/new` | seller | form |
| POST `/seller/listings` | seller, CSRF | `create_listing`; validation error re-renders (400) |
| GET `/seller/listings/{id}/edit` | seller (owner) | form |
| POST `/seller/listings/{id}` | seller, CSRF | `update_listing` (title, description, photo URLs) |
| POST `/seller/listings/{id}/active` | seller, CSRF | `update_listing(active=…)` |
| POST `/seller/availability` | seller, CSRF | `post_availability` / `delete_availability` |
| POST `/seller/payments/onboard` | seller, CSRF | Decision 12 |
| GET `/onboarded` | anyone | 303 `/seller` |
| POST `/logout` | anyone with a cookie, CSRF | unchanged otherwise |
| GET `/verify`, POST `/verify`, GET `/verify/done` | anyone | Decision 11 |
| POST `/verify/resend` | buyer or seller, CSRF | resend; 303 back |
| GET `/password-reset`, POST `/password-reset/request`, GET `/password-reset/sent` | anyone | Decision 11 |
| GET/POST `/password-reset/confirm` | anyone | Decision 11 |

View families: buyer pages render `QuoteOut`, `BuyerJobView` and the public
`CatalogListingOut`/`CatalogSellerOut`; seller pages render
`SellerOfferView`, `SellerJobView`, `SellerListingOut`, `SellerProfileOut`.
A buyer page never shows `seller_payout` or margin; a seller page never shows
`buyer_price`, margin, or buyer identity (no buyer id or name; a job is
identified by its id and service type). Templates receive view objects, not
ORM rows (the nav's `current_user` is the one existing exception, unchanged:
display name and role of the viewer themselves).

## Notifications

None new. The existing enqueues inside the services fire as on the API.

## Settings

- `stripe_publishable_key: str = ""` — startup refuses `STRIPE_SECRET_KEY`
  without it, and refuses a value that does not start with `pk_` (a secret
  or restricted key pasted here would be published in page source).
- `require_verified_email: bool = False`.
- `fake_charges_pending: bool = False` — refused alongside a Stripe key.

`.env.example` gains the three lines, and the `BASE_URL` note is corrected
(the app now serves `/verify` and `/password-reset/confirm`).

## Error handling

Provider failures stay 502 `ProviderUnavailableError` in the service; the
page rolls back and redirects with a "payment provider unavailable, try
again" notice. Page dependency failures (login required, wrong role, CSRF)
are `PageInterrupt`s raised before the handler, so nothing commits.

## Testing

- `tests/test_booking_pages.py`: buyer flow end to end through forms
  (quote → book → booking page states → cancel), double-submit of book,
  expired quote, unknown/other buyer's quote and booking (404), the payment
  step in fake mode (no Stripe script) and with a scripted Stripe-named
  provider (key and secret present only for the owner while pending, gone
  after the webhook), the status fragment and `HX-Refresh`.
- `tests/test_seller_pages.py`: dashboard sections; accept (instant and
  pending charge), decline, complete; listing create/edit/toggle;
  availability; onboarding; the rollback rule (an expired offer accepted
  through the page stays OFFERED, as on the API).
- `tests/test_csrf.py`: derivation properties (differs from `token_hash`,
  differs per session), and for each money-moving form (quote, book, cancel,
  accept, decline, complete) a forged POST with a valid cookie and a missing
  or wrong token is 403 with no state change; logout requires the token.
- `tests/test_verify_gate.py`: the gate off by default; on, the three
  actions 403 over JSON and the pages; verified users pass; verify and reset
  pages; reset request uniform; tokens absent from access logs.
- `tests/test_pages.py`: source-asymmetry scans extended to every new page
  (buyer pages scan for `seller_payout`/margin/payout figures; seller pages
  for `buyer_price` and the buyer's price figure and identity), and
  attribute-context XSS checks where seller or buyer text lands in an
  attribute (listing edit form `value=`, the quote page).
- `tests/test_architecture.py`: every POST route in the pages package
  depends on `require_csrf` except the named allowlist, and the allowlist is
  exactly the unauthenticated forms.
- Settings validator tests; idempotency: a page POST with a key is never
  stored.
- Suite green on SQLite and Postgres; `gate.sh --pg`. Browser run with
  Playwright against a live server, screenshots in `docs/screenshots/`.

## Non-goals

Dispatch booking from a page; reviews and disputes from pages; seller
capacity editing; buyer profile page; admin pages (admin stays API-only);
login CSRF (Decision 6); rate limiting; live Stripe verification.

## Constraints carried forward

Identity from the principal only; role-scoped views, never dicts or ORM rows
in a role's context; `CommitRoute` on every router, commit before response,
no commit in teardown; pages never import `api`/`routers`; catalog truth in
`catalog.py`; autoescape on, no `|safe`; public page source carries no
payout, margin, capacity, `payments_ready` or email; providers only through
the port, no `import stripe` outside the adapter; access logs carry no
query, header or body; money stays `Decimal`; pinned jobs never re-match.
