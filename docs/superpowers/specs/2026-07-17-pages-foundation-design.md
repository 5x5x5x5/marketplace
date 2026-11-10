# Pages foundation — design (market-platform fork, phase 2)

> **Note (added later):** this phase was built in a separate fork named `market-platform`, which was later merged back. The phase is part of this repository; the fork, `upstream`/`origin` remotes and fork-specific database names mentioned below no longer exist.

**Date:** 2026-07-17
**Status:** Approved under the maintainer's standing authorization ("go ahead
with phase 2"; frontend approach HTMX/Jinja-in-the-monolith decided
2026-07-16). Scope: the server-rendered foundation + PUBLIC pages + AUTH
pages. Booking flow, buyer/seller dashboards, and payment/token pages are
phase 3 (transact pages) — explicitly out of scope here.

## Problem

The fork is API-only. A browse marketplace's storefront is its public
catalog — buyers must be able to see listings, seller profiles, and prices in
a browser, and sign up/log in, without Swagger. Phase 1 built the catalog
API; this phase gives it a face and gives sessions a cookie so browsers can
hold them.

## Decisions (maintainer-delegated, recommended options)

1. **Pages live in the monolith** as a new `pages.py` router mounted at `/`
   (not `/v1` — pages are HTML, not API). Same process, same ops machinery
   (request-ids, envelope, access logs, CommitRoute) for free.
2. **Shared read logic moves to a new `catalog.py` module** — the catalog
   query/pricing helpers currently in `api.py` (`_visible_listings_stmt`,
   `_listing_bookable`, `_indicative_buyer_price`, the row→view builders)
   are needed by both the JSON API and the pages. Extracting them avoids an
   api↔pages import cycle. Pure refactor: zero behavior change, proven by
   the untouched test suite.
3. **Cookie sessions reuse `auth_sessions` wholesale.** Login/signup pages
   create the SAME opaque token rows the API uses and set the raw token in
   an `HttpOnly` cookie (`session_token`); `_principal` gains a cookie
   fallback (Authorization header wins when both present). Logout deletes
   the row and clears the cookie. Nothing new to revoke or sweep — cookies
   are just another carrier for the existing revocable sessions.
4. **CSRF posture: `SameSite=Lax` + POST-only mutations, documented.** Lax
   stops cross-site POSTs from carrying the cookie, which is the pilot-grade
   baseline; no token machinery yet (revisit in phase 3 when money-moving
   forms appear — booking POSTs will get CSRF tokens there if warranted).
   `Secure` flag via `settings.cookie_secure` (default False for local dev;
   True in production — documented).
5. **Progressive enhancement, hand-rolled CSS.** Forms are plain HTML that
   work without JavaScript; htmx (vendored `static/htmx.min.js`, no CDN —
   the CSP-friendly, offline-friendly choice) enhances where it's cheap.
   One stylesheet, light-editorial: cream background, ink text, serif
   display headings, a single accent color. No CSS framework, no build step.
6. **Jinja autoescape is a security boundary.** Listing titles/descriptions
   are seller-supplied content rendered on unauthenticated pages —
   `Jinja2Templates` autoescaping stays on, and an XSS regression test
   renders a `<script>`-bearing listing title and asserts it comes out
   escaped.
7. **The CommitRoute invariant widens to the whole app.**
   `_assert_commit_routes` currently checks `/v1` paths; pages write too
   (login/signup create session rows). New rule: every `APIRoute` in the app
   must be a `CommitRoute` except an explicit exempt list (`/healthz`).
   Static files are a `Mount`, not an `APIRoute` — unaffected.

## Design

### 1. Dependencies + scaffolding

- `uv add jinja2 python-multipart` (Form(...) parsing needs multipart).
- `src/marketplace/pages.py`: `templates = Jinja2Templates(directory=...)`,
  `pages_router = APIRouter(route_class=CommitRoute, tags=["pages"])`,
  included in `api.py`'s assembly; `app.mount("/static", StaticFiles(...))`.
- `src/marketplace/templates/`: `base.html` (header with nav + auth state,
  footer), `index.html`, `listing.html`, `seller.html`, `signup.html`,
  `login.html`, plus a `_listing_card.html` partial.
- `src/marketplace/static/`: `style.css`, `htmx.min.js` (vendored).
- Templates/static ship inside the package directory so the installed app
  stays self-contained.

### 2. `catalog.py` extraction (pure refactor)

Move from `api.py`, byte-equivalent behavior: `listing_bookable(session,
listing)`, `visible_listings_stmt()`, `indicative_buyer_price(session,
service_type_id)`, and the catalog row→dict/view builders. `api.py` imports
them (names updated); the catalog endpoint tests and the parity test pass
UNMODIFIED (that is the refactor's proof). `pages.py` imports the same
functions — one source of truth, no cycle.

### 3. Cookie auth

- `settings`: `cookie_secure: bool = False` (+ comment: set True behind
  TLS in production).
- `auth.py`: `_principal` becomes header-first, cookie-fallback:

```python
def _principal(
    session: _SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    session_token: Annotated[str | None, Cookie()] = None,
) -> Claims:
    authorization = f"{credentials.scheme} {credentials.credentials}" if credentials else ""
    claims = _resolve_bearer(session, authorization)
    if claims is None and session_token:
        claims = _resolve_bearer(session, f"Bearer {session_token}")
    if claims is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return claims
```

  Header-wins ordering is deliberate (an API client with a stale browser
  cookie must not be silently downgraded). The cookie carries the RAW token
  (same value the JSON API returns); at rest the DB still stores only the
  sha256 — the cookie is the browser-side equivalent of the client holding
  its bearer token.
- Page auth flows (in `pages.py`, calling `auth.py` functions directly —
  signup/login logic is already factored there; VERIFY the exact callables
  and reuse, never duplicate password/session logic):
  - `GET /signup`, `GET /login` — forms (role picker on signup:
    buyer/seller).
  - `POST /signup`, `POST /login` — on success create the session row, set
    the cookie (`HttpOnly`, `SameSite=Lax`, `Secure` per settings,
    `max_age` = session TTL), 303 → `/`. On failure re-render the form with
    a generic error (no account-enumeration copy — mirror the API's
    uniform-401 stance).
  - `POST /logout` — delete the session row (reuse the logout deletion
    logic), clear the cookie, 303 → `/`.
- `base.html` nav shows login/signup links or a "logged in as
  {display_name} ({role})" + logout button — via an optional-principal
  helper (`Claims | None`) that must NOT 401 on public pages.

### 4. Public pages

- `GET /` — the catalog: listing cards (first photo or a neutral
  placeholder block, title, seller display name + rating, `price_from`
  labeled "from"), optional `?service_type_id=` filter links, paginated
  (reuse Limit/Offset semantics via query params).
- `GET /listings/{listing_id}` — photos, description, seller card (link to
  profile), price_from, and a "Book" button that is a **stub in phase 2**:
  links to `/login` when anonymous, and renders a "booking arrives in
  phase 3" note when logged in (the API booking flow exists; the PAGE flow
  is phase-3 scope — do not half-build it).
- `GET /sellers/{seller_id}` — profile: display name, rating, completed
  jobs, their visible listings as cards.
- All three render ONLY catalog-public data (the `catalog.py` helpers).
  The asymmetry doctrine extends to HTML: a page-source scan test asserts
  no payout/margin/capacity/email/payments_ready strings appear.
- 404s render a minimal error page (template, not JSON) for unknown/
  invisible listings/sellers — same visibility rules as the JSON API.

### 5. Invariant widening

`_assert_commit_routes` (api.py): drop the `/v1` prefix filter; assert every
`APIRoute` is a `CommitRoute` unless its path is in
`_COMMIT_EXEMPT = {"/healthz"}`. The rogue-router unit test gains a non-/v1
case.

## Testing

- Refactor proof: catalog/parity/asymmetry tests pass UNMODIFIED.
- Cookie auth: login sets HttpOnly+Lax cookie; cookie-only request reaches
  an authed endpoint; header beats cookie when both present (stale-cookie
  test); logout deletes the row AND clears the cookie (subsequent
  cookie-only request 401s); signup sets cookie and creates the user.
- Pages: 200 + key content for /, listing detail, seller profile; 404 page
  for hidden/unknown; filter + pagination params respected; nav reflects
  auth state (anonymous vs logged-in markup).
- XSS: listing titled `<script>alert(1)</script>` renders escaped
  (`&lt;script&gt;` present, raw tag absent).
- HTML asymmetry scan on all public pages (payout/margin/capacity/email/
  payments_ready absent from page source).
- Invariant: non-/v1 rogue route caught by the widened assert.
- Full suites both backends; demo unchanged (24 steps — pages don't touch
  the API demo); live_server smoke: fetch `/` over the socket, 200 +
  text/html.

## Non-goals (phase 3+ or later)

- Booking/checkout pages, buyer/seller dashboards, offer inbox, payment
  (Stripe Elements) and email-token pages — phase 3.
- CSRF token machinery (Lax posture documented; tokens arrive with phase-3
  money forms if warranted).
- Search beyond the service-type filter; SEO meta beyond title tags; slugs.
- Photo uploads (URLs remain), maps, calendars.
- Admin pages (Swagger stays the admin console).
- Real-mail configuration / verification gate (still deployment-time).
