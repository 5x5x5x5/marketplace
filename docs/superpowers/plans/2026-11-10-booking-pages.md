# Booking pages Implementation Plan

**Goal:** a buyer can quote, book, pay, follow and cancel, and a seller can
accept, decline, complete and manage listings, entirely through
server-rendered pages, with CSRF tokens on every authenticated form.

**Architecture:** `pages.py` becomes the `pages/` package (common, csrf,
storefront, account, buyer, seller). Pages call `services/` and `auth` and
render role-scoped view models; they add no domain logic. Small reads are
added to the owning service modules. The payment port gains `browser_key`;
the Payment Element lives in `static/payment.js`. The verification gate is a
service-layer check behind a setting.

**Spec:** `docs/superpowers/specs/2026-11-10-booking-pages-design.md` (approved). Branch: `wt/pages`.

## Global Constraints

- Gate after every task, bare exit codes, never piped:
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`.
  Pyright strict.
- Failing test first; run it and keep the red output for the task report.
- Zero new dependencies (htmx is already vendored; Stripe.js loads from
  js.stripe.com in the browser, not as a package).
- Stage exact paths; never `git add -A`.
- Phase invariants (spec "Constraints carried forward" plus): every page
  POST outside the allowlist depends on `csrf.require_csrf`; a page handler
  that catches `DomainError` rolls back before answering; notices are codes;
  no `buyer_id`/`seller_id` form fields; pages never import `api`/`routers`
  or `stripe`; templates get view models.
- Do not edit `CLAUDE.md`, `README.md`, `SECURITY.md`, `ROADMAP.md`,
  `AGENTS.md` (the coordinator integrates docs from the final report).
- Before the branch is done: gate green on SQLite and Postgres
  (`.claude/skills/gate/gate.sh --pg`), and the browser run passes.

---

### Task 1: Spec and plan

**Files:** Create both documents. Commit: "Add the booking pages design spec and plan".

---

### Task 2: Split pages into a package; page plumbing; idempotency skips page POSTs

**Files:**
- Delete: `src/marketplace/pages.py`
- Create: `src/marketplace/pages/{__init__,common,storefront,account}.py`
- Modify: `src/marketplace/api.py` (register the `PageInterrupt` handler;
  pass `pages.is_page_post` to `IdempotencyMiddleware`),
  `src/marketplace/idempotency.py` (replace `_PAGE_AUTH_PATHS` with the
  injected predicate)
- Test: `tests/test_idempotency.py`

**Interfaces produced:**
- `pages.pages_router`, `pages.is_page_post(path: str) -> bool`,
  `pages.common.page_interrupt_handler`.
- `common.templates`, `common.render(request, name, ctx, status_code=200) ->
  HTMLResponse` (adds `Referrer-Policy: same-origin`),
  `common.nav_context(session, claims, request)`,
  `common.PageInterrupt(response_factory)`, `common.LoginRequired`,
  `common.PageForbidden(message)`, `common.page_buyer`/`page_seller`
  dependencies (`BuyerPage`, `SellerPage` annotated aliases),
  `common.redirect(url, notice=None)`,
  `common.redirect_after_error(session, url, exc, notices_by_status)`,
  `common.notice_message(code) -> str | None`.
- `IdempotencyMiddleware(app, exempt: Callable[[str], bool] = ...)`.

Steps:
1. Failing test `test_page_post_with_key_is_never_stored`: logged-in buyer
   POSTs `/logout` with `Idempotency-Key`; no `IdempotencyRecord` row for
   `/logout`; and `is_page_post("/bookings/<uuid>/cancel")` is true once that
   route exists (asserted in Task 5), false for `/v1/jobs`.
2. Move storefront and account handlers verbatim; templates directory
   resolves from the package parent.
3. Gate. Commit: "Split pages into a package and exempt page POSTs from replay".

---

### Task 3: CSRF tokens

**Files:**
- Create: `src/marketplace/pages/csrf.py`, `tests/test_csrf.py`
- Modify: `pages/common.py` (nav exposes `csrf_token` when a cookie session
  exists), `pages/account.py` (logout requires CSRF), `templates/base.html`
  (logout form hidden field), `tests/test_auth.py`
  (`test_logout_page_deletes_row_and_clears_cookie` sends the token: a
  deliberate change), `tests/test_architecture.py`

**Interfaces produced:** `csrf.token_for(raw_session_token: str) -> str`,
`csrf.require_csrf` (dependency; raises `PageForbidden` on missing cookie,
missing field or mismatch, compared with `hmac.compare_digest`),
`csrf.CSRF_EXEMPT_POSTS = frozenset({"/signup", "/login", "/verify",
"/password-reset/request", "/password-reset/confirm"})`, form field name
`csrf_token`.

Steps:
1. Failing tests: `token_for(raw) != sha256(raw)` and differs per token;
   logout without token is 403 and the session survives; logout with the
   token works; architecture test
   `test_every_page_post_requires_csrf_except_the_allowlist` (walks
   `route.dependant` recursively for `require_csrf`; asserts the allowlist
   equals the exact set above).
2. Implement. Gate. Commit: "Require a session-bound CSRF token on authenticated page forms".

---

### Task 4: Service reads, view fields, verification gate

**Files:**
- Modify: `services/booking.py` (`get_quote`, `job_for_quote`, gate in
  `create_job`), `services/jobs.py` (gate in `accept_offer`),
  `services/listings.py` (`get_own_listing`, gate in `create_listing`),
  `services/profiles.py` (`availability_by_service_type`),
  `services/common.py` (`require_verified_email`), `errors.py`
  (`EmailNotVerifiedError(ForbiddenError)`), `catalog.py`
  (`public_seller`), `models.py` (`BuyerJobView.pinned_seller_id`,
  `SellerProfileOut.payments_ready`), `settings.py`
  (`require_verified_email`), `auth.py` (`resend_verification(db, mail,
  user_id)`)
- Test: `tests/test_verify_gate.py`

Steps:
1. Failing tests: gate off → unverified buyer books; gate on → `POST /v1/jobs`,
   `POST /v1/seller/offers/{id}/accept`, `POST /v1/seller/listings` 403 with
   `verify your email address first` and no state change (quote still
   present, offer still OFFERED); verified → allowed; new view fields present.
2. Implement. Gate. Commit: "Gate booking, accepting and listing on a verified email, behind a setting".

---

### Task 5: Buyer pages

**Files:**
- Create: `pages/buyer.py`, templates `quote.html`, `bookings.html`,
  `booking.html`, `_booking_status.html`; Modify `listing.html` (booking
  form for buyers), `base.html` (nav links), `static/style.css`
- Test: `tests/test_booking_pages.py`

Steps:
1. Failing tests (helpers sign a buyer up via the page and read the CSRF
   token from rendered HTML): listing page shows the quote form only to a
   buyer; quote → quote page shows price and expiry; book → booking page
   "Waiting for"; seller accepts via API → "Confirmed"; complete → "Completed";
   cancel from pending → "Cancelled"; second book submit lands on the same
   booking; expired quote → 410 page; other buyer → 404; seller → 403;
   anonymous → 303 /login; `/bookings` lists the job; status fragment returns
   `HX-Refresh` when status moved.
2. Implement with `redirect_after_error`. Gate. Commit: "Add buyer booking pages: quote, confirm, follow, cancel".

---

### Task 6: Payment step

**Files:**
- Modify: `payments/port.py` (`browser_key`), `payments/fake.py`
  (`browser_key = None`; pending mode), `payments/stripe_provider.py`
  (`publishable_key` arg), `payments/__init__.py`, `settings.py`
  (`stripe_publishable_key`, `fake_charges_pending`, validator),
  `templates/booking.html`, Create `static/payment.js`, `.env.example`
- Test: `tests/test_booking_pages.py`, `tests/test_settings_validation.py`

Steps:
1. Failing tests: fake mode pending charge → page says payment pending, no
   `js.stripe.com`, no `payment.js`; with `fake_provider.browser_key`
   scripted to `pk_test_…` → page carries `data-publishable-key`,
   `data-client-secret` and the Stripe script for the owner only; seller
   dashboard and other buyer never see the secret; after the
   `payment_succeeded` webhook the script and secret are gone; validator
   refuses Stripe key without publishable key, refuses a non-`pk_` value,
   refuses `fake_charges_pending` with Stripe; `fake_charges_pending` leaves
   charges pending.
2. Implement. Gate. Commit: "Add the payment step: Stripe Payment Element when configured, pending state with the fake".

---

### Task 7: Seller dashboard

**Files:** Create `pages/seller.py`, templates `seller_dashboard.html`,
`listing_form.html`; Test `tests/test_seller_pages.py`.

Steps:
1. Failing tests: dashboard sections; onboard → ready notice; availability
   on/off (repeat off is fine); create listing (validation error re-renders
   400 with the entered values escaped); edit; active toggle false/true;
   accept (instant) → job listed accepted; accept with pending charge →
   "awaiting payment"; decline → offer gone, pinned job expired; complete →
   completed; expired offer accept via page leaves the offer OFFERED
   (rollback rule); buyer → 403.
2. Implement. Gate. Commit: "Add the seller dashboard: offers, jobs, listings, availability, onboarding".

---

### Task 8: Verify, reset and onboarding-return pages

**Files:** Modify `pages/account.py`; templates `verify.html`,
`message.html`, `reset_request.html`, `reset_confirm.html`; Test
`tests/test_verify_gate.py`.

Steps:
1. Failing tests: GET `/verify?token=` does not consume; POST verifies and
   redirects; bad token 400 generic; resend issues a new mail (CSRF
   required); reset request for known and unknown email answer identically
   (status, location); confirm resets password, revokes sessions, clears
   cookie; bad token generic 400; short password re-renders; access log of
   `/verify?token=SECRET` and `/password-reset/confirm?token=SECRET` never
   contains `SECRET`; `GET /onboarded` → `/seller`; gate notices on pages.
2. Implement. Gate. Commit: "Add verify-email and password-reset pages".

---

### Task 9: Asymmetry and XSS scans over every new page

**Files:** `tests/test_pages.py`.

Steps: scan rendered buyer pages (quote, booking in each state, bookings)
for `seller_payout`, `margin`, `payout`, and the seller's payout figure;
seller pages for `buyer_price`, `margin`, the buyer's price figure, the
buyer's email and display name; public pages unchanged set. Attribute-context
XSS: hostile listing title in the edit form `value=` and in the quote page.
Commit: "Extend the page-source asymmetry and XSS scans to the booking pages".

---

### Task 10: Browser run, findings, screenshots, final review

Playwright script (scratchpad, not committed) against uvicorn on a seeded
scratch DB with the fake provider, `ALLOW_UNSIGNED_WEBHOOKS=true`,
`FAKE_CHARGES_PENDING=true`. Screenshots at device scale factor 2 to
`docs/screenshots/`. Findings in `docs/tryout-findings-2026-11-10.md`. Self
review against `money-path-reviewer` and `asymmetry-reviewer` checklists;
fix wave; `gate.sh --pg`.

## Self-review

- Spec coverage: Decisions 1–2 → T2; 3–5 → T2/T5/T7; 6 → T3; 7 → T2;
  8, 15 → T6; 9 → T5; 10 → T4; 11 → T8; 12 → T7/T8; 13 → T4; 14 → T2.
- Names: `require_csrf`, `CSRF_EXEMPT_POSTS`, `token_for`,
  `redirect_after_error`, `PageInterrupt`, `browser_key`,
  `require_verified_email`, `EmailNotVerifiedError` are used identically in
  every task.
- Judgment calls: tests are specified by name and assertion rather than
  written out in full here, because each task's tests are written first in
  the task itself; the notice-code table lives in `pages/common.py`.
- Placeholders: none.

## Final-review amendments

Self-review of the whole branch against `money-path-reviewer` and
`asymmetry-reviewer` (the coordinator runs the reviewers independently):

1. **Concurrent double confirm had only a sequential test.** The page's
   fallback (a confirm that finds its quote gone redirects to the job made
   from it) depends on the second submit blocking on the quote's row lock and
   then reading the committed job. Added the Postgres-only
   `test_concurrent_double_confirm_lands_both_on_one_booking` (threads, a
   barrier, five attempts); without the fallback the loser lands on "no
   longer available" and the test fails.
2. **Try-out T1** (catalog cards had no link to the listing) fixed with
   `test_catalog_cards_link_to_their_listing`; see
   `docs/tryout-findings-2026-11-10.md`.
3. **Accepted, not changed:** the booking page's 4 s status poll calls
   `jobs.get_job_buyer`, which runs the lazy sweep, exactly as a JSON client
   polling `GET /v1/jobs/{id}` does; the seller dashboard shows the newest 24
   jobs without paging (offers and listings are complete).

Deviations from the plan: the payment step's port change is one attribute,
`browser_key`; `tests/test_capacity.py` patches the page size where it now
lives; the CSRF allowlist check landed in two steps (Task 3 without, Task 8
with, the "every allowlisted path exists" assertion); the settings validator
tests live in `tests/test_payment_settings.py`, not the file the plan named; an
extra module, `pages/interrupts.py`, holds the page interrupt types and their
handler, so `csrf.py` can raise one without importing `common`; `nav_context`
takes no `request` argument; and the Task 2 test was first written as a
predicate check on the exemption and completed as an end-to-end test in the
review fix wave.
