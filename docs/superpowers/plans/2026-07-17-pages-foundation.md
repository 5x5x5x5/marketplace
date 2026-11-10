# Pages Foundation Implementation Plan (market-platform fork, phase 2)

> **Note (added later):** this phase was built in a separate fork named `market-platform`, which was later merged back. The phase is part of this repository; the fork, `upstream`/`origin` remotes and fork-specific database names mentioned below no longer exist.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Server-rendered HTMX/Jinja foundation in the monolith: public catalog/listing/seller pages, cookie sessions over the existing `auth_sessions`, signup/login/logout pages — with the catalog logic extracted to one shared module and the CommitRoute invariant widened to every route.

**Architecture:** New `catalog.py` holds the shared read logic (pure extraction from api.py — proven by unmodified tests). New `pages.py` router (`route_class=CommitRoute`, mounted at `/`) renders Jinja templates and reuses `auth.py`'s session logic; the cookie (`session_token`, HttpOnly, SameSite=Lax) carries the same raw bearer token the JSON API issues, with header-wins fallback in `_principal`. Progressive enhancement: plain-HTML forms, vendored htmx, one hand-rolled light-editorial stylesheet.

**Tech Stack:** FastAPI + Jinja2Templates (`jinja2`, `python-multipart` — NEW deps via `uv add`), StaticFiles mount, existing CommitRoute/observability machinery.

**Spec:** `docs/superpowers/specs/2026-07-17-pages-foundation-design.md` (approved).

## Global Constraints

- `uv` only from the repository root. **Bare exit codes.**
- Pyright strict 0; ruff clean; formatter strips not-yet-used imports (import + first use SAME edit).
- TDD binding and audited: failing tests first, RUN, quote red output.
- Fork PG: `postgresql+psycopg://marketplace:marketplace@localhost:5432/market_platform`. **NEVER `docker compose down -v` (shared container). Never bind port 8000.** The container is usually already up — do not race `docker compose up` from this dir if it is.
- **Import direction (no cycles):** `pages.py` imports from `db.py` (get_session/CommitRoute), `auth.py`, `catalog.py`, `entities/models` — NEVER from `api.py`. `api.py` includes `pages_router` and imports `catalog.py`.
- **Jinja autoescape stays on** (seller-supplied content on unauthenticated pages — XSS boundary, tested).
- **HTML asymmetry doctrine:** public page source never contains payout/margin/capacity/email/payments_ready — tested by scan.
- House lessons apply to pages too: flush before returning created ORM state that gets serialized; CommitRoute never persists 4xx (error re-renders must not need persistence — they don't here).
- Commit trailer: `Co-Authored-By: <agent attribution trailer>`. No backticks in double-quoted `git commit -m`. Commit ONLY listed files.
- Branch `pages-foundation` (exists, spec committed).

---

### Task 1: catalog.py extraction + scaffolding + landing page + widened invariant

**Files:**
- Create: `src/marketplace/catalog.py`, `src/marketplace/pages.py`, `src/marketplace/templates/base.html`, `src/marketplace/templates/index.html`, `src/marketplace/templates/_listing_card.html`, `src/marketplace/templates/error.html`, `src/marketplace/static/style.css`, `src/marketplace/static/htmx.min.js`
- Modify: `src/marketplace/api.py` (imports switch to catalog.py; include pages_router + static mount; `_assert_commit_routes` widened), `pyproject.toml`/`uv.lock` (via `uv add jinja2 python-multipart`)
- Test: `tests/test_pages.py` (new)

**Interfaces:**
- Consumes: api.py's current `_listing_bookable` (~2272), `_visible_listings_stmt`, `_indicative_buyer_price` (~564), catalog row→view builders (~2263-2325) — MOVE these (rename without the underscore: `listing_bookable`, `visible_listings_stmt`, `indicative_buyer_price`, plus the builders) into `catalog.py`; api.py imports them; NO logic edits (the unmodified catalog/parity tests are the refactor proof).
- Produces (Tasks 2-3 rely on): `catalog.py` public functions; `pages.py` with `templates` (Jinja2Templates), `pages_router` (CommitRoute), `GET /` rendering the catalog grid; `app.mount("/static", ...)`; widened `_assert_commit_routes` with `_COMMIT_EXEMPT = {"/healthz"}`; `base.html` blocks (`title`, `content`) + nav that renders login/signup links when anonymous (auth-state wiring completes in Task 3 — render the anonymous variant unconditionally for now with a TODO-free conditional on a `claims` context var defaulting to None).

- [ ] **Step 1: Write the failing tests** — `tests/test_pages.py`:

```python
"""Server-rendered pages: catalog grid, shared-logic extraction, invariant."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _listed_seller(client: TestClient, auth: AuthFactory, sid: str, title: str = "City rides") -> None:
    onboard_and_avail(client, auth, sid, "s1")
    r = client.post(
        "/v1/seller/listings",
        json={"service_type_id": sid, "title": title, "description": "Vetted drivers"},
        headers=auth("seller", "s1"),
    )
    assert r.status_code == 201, r.text


def test_landing_renders_catalog(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "City rides" in r.text
    assert "from" in r.text  # price_from label
    assert "20.00" in r.text


def test_landing_empty_ok(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200


def test_landing_hides_invisible(client: TestClient, basic_service: str, auth: AuthFactory, admin: Header) -> None:
    _listed_seller(client, auth, basic_service)
    lid = client.get("/v1/seller/listings", headers=auth("seller", "s1")).json()[0]["id"]
    assert client.post(f"/v1/admin/listings/{lid}/hide", headers=admin).status_code == 200
    assert "City rides" not in client.get("/").text


def test_page_source_asymmetry(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    html = client.get("/").text.lower()
    for forbidden in ("seller_payout", "margin", "capacity", "payments_ready", "@"):
        assert forbidden not in html, forbidden


def test_xss_listing_title_escaped(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service, title="<script>alert(1)</script>")
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_static_served(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_commit_route_invariant_covers_pages() -> None:
    import pytest
    from fastapi import APIRouter, FastAPI

    from marketplace import api

    rogue_app = FastAPI()
    rogue = APIRouter()  # NOT /v1 — the widened invariant must still catch it

    @rogue.get("/rogue-page")  # pyright: ignore[reportUnusedFunction]
    def rogue_page() -> dict[str, str]:
        return {}

    rogue_app.include_router(rogue)
    with pytest.raises(RuntimeError, match="CommitRoute"):
        api._assert_commit_routes(rogue_app)
```

Adaptation notes: the `"@"` in the asymmetry scan guards email leakage — if base.html legitimately needs an `@` (it shouldn't in phase 2), swap for explicit email-string checks and say so. The refactor itself has no new tests: `tests/test_catalog.py` passing UNMODIFIED is the proof (state it in the report with the run output).

- [ ] **Step 2: Red run** (`uv run pytest tests/test_pages.py -v` → 404s/asserts; capture).

- [ ] **Step 3: Deps + extraction.** `uv add jinja2 python-multipart`. Create `catalog.py` with a module docstring ("shared catalog read logic — one source of truth for the JSON API and the HTML pages") and MOVE the four helper groups from api.py verbatim (drop the leading underscores; keep the memoized price pattern). Update api.py call sites/imports. Run `tests/test_catalog.py` — must pass with ZERO edits to it.

- [ ] **Step 4: pages.py + templates + static.**

```python
"""Server-rendered pages: the browse storefront (phase 2 foundation).

Import direction: pages -> (db, auth, catalog, entities, models). NEVER
import api (api includes this router)."""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import catalog
from .db import CommitRoute, get_session

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
pages_router = APIRouter(route_class=CommitRoute, tags=["pages"])

_SessionDep = Annotated[Session, Depends(get_session)]


@pages_router.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    session: _SessionDep,
    service_type_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    ...  # catalog.visible listings via the shared stmt, 24/page, build cards
    return templates.TemplateResponse(request, "index.html", {...})
```

(Adapt to the real shapes of the moved builders — the brief's code is directional; the moved functions are the source of truth.) `base.html`: semantic HTML, `{% block title %}`/`{% block content %}`, nav with `{% if claims %}…{% else %}login/signup links{% endif %}` where `claims` defaults to `None` in every phase-2 context dict. `style.css`: light editorial — cream `#faf6ef`-family background, near-black ink, serif headings (`Georgia, 'Times New Roman', serif` stack), ONE accent (a deep green or oxblood — pick one and use it for links/buttons only), generous whitespace, cards with hairline borders, `max-width: 72rem` shell. No framework. `htmx.min.js`: vendor the current 1.9.x/2.x minified file (download once; commit it; note version in the report).

- [ ] **Step 5: api.py assembly.** Include `pages_router` + `app.mount("/static", StaticFiles(directory=...), name="static")` (import in same edit) BEFORE `_assert_commit_routes(app)`; widen the invariant: remove the `startswith("/v1")` filter, add `_COMMIT_EXEMPT = {"/healthz"}`, skip non-`APIRoute` routes (Mounts). Keep the lifespan re-assert.

- [ ] **Step 6: Gates** (tests/test_pages.py, tests/test_catalog.py unmodified-green, ruff+format, pyright, full SQLite, full fork-PG) **+ Commit** (all created files + api.py + pyproject.toml + uv.lock; message "Add server-rendered catalog landing: catalog.py extraction, pages scaffold, widened invariant").

---

### Task 2: listing detail + seller profile pages

**Files:**
- Create: `src/marketplace/templates/listing.html`, `src/marketplace/templates/seller.html`
- Modify: `src/marketplace/pages.py`
- Test: `tests/test_pages.py` (append)

**Interfaces:** consumes Task 1's `catalog.py`/templates; produces `GET /listings/{listing_id}` and `GET /sellers/{seller_id}` with the JSON API's exact visibility rules (404 → `error.html` page, status 404, text/html).

- [ ] **Step 1 (red):** tests — detail 200 with title/description/photos/seller-link/price; hidden/unknown listing → 404 HTML page; seller profile 200 with rating + listing cards; suspended seller → 404; **"Book" stub**: anonymous detail page contains a link to `/login`; the phase-3 note text appears instead of any booking form (no `/v1/quotes` form action anywhere in phase-2 HTML — assert its absence); XSS + asymmetry scans extended to both new pages.
- [ ] **Step 2:** implement (reuse catalog.py detail/profile fetchers; photos render as `<img>` list with the first as hero; missing photos → neutral CSS block). Error page handler: raise `HTTPException(404)` and register a pages-side exception renderer? NO — keep it simple: the handlers return `templates.TemplateResponse(request, "error.html", {...}, status_code=404)` directly on the not-visible path (no exception machinery; CommitRoute's <400 guard skips commit on 404 — verify nothing needed persisting, which is true for pure reads).
- [ ] **Step 3:** gates + commit ("Add listing detail and seller profile pages").

---

### Task 3: cookie sessions + auth pages + nav state

**Files:**
- Modify: `src/marketplace/auth.py` (`_principal` cookie fallback; `optional_claims` dependency), `src/marketplace/settings.py` (`cookie_secure: bool = False`), `src/marketplace/pages.py` (+auth routes), `src/marketplace/templates/base.html` (nav auth state), create `src/marketplace/templates/signup.html`, `src/marketplace/templates/login.html`
- Test: `tests/test_pages.py` (append) + `tests/test_auth.py` (append cookie tests)

**Interfaces:**
- Consumes: auth.py's existing signup/login/logout logic — READ the module first and reuse the exact functions the JSON endpoints use (user creation, password verify, session issue/delete); NEVER duplicate hashing/session logic.
- Produces: `_principal` header-first cookie-fallback (spec §3 code); `auth.optional_claims(...) -> Claims | None` (same resolution, returns None instead of 401 — for nav state on public pages); `GET/POST /signup`, `GET/POST /login`, `POST /logout` page routes; cookie contract: name `session_token`, HttpOnly, `SameSite=Lax`, `Secure=settings.cookie_secure`, `max_age=settings.session_ttl_hours*3600`, path `/`; success → 303 to `/`; failure → re-rendered form, generic error, NO account-enumeration copy; logout deletes the session row then clears the cookie, 303 `/`.

- [ ] **Step 1 (red):** tests: login sets the cookie with the exact attribute set (parse Set-Cookie); cookie-only request reaches `GET /v1/auth/me` (proves the fallback covers the whole API surface); header-beats-cookie (stale cookie + valid header of a DIFFERENT user → header identity wins); logout row-deleted + cookie cleared + subsequent cookie-only 401; signup page creates user+cookie; bad login re-renders with generic error (assert the same body for wrong-password vs no-such-user); nav shows display_name+logout when cookied, login/signup links when not.
- [ ] **Step 2:** implement. Pages POST handlers use `Form(...)` params (python-multipart). The 303 RedirectResponse must carry the Set-Cookie — set it on the RedirectResponse object. CommitRoute commits 303s (<400) — the session row persists; VERIFY by the cookie-only follow-up request in tests (this is the F2-class ordering guarantee doing its job).
- [ ] **Step 3:** gates (incl. FULL suite — the `_principal` signature change ripples nowhere because both new params default) + commit ("Add cookie sessions and auth pages").

---

### Task 4: docs + gates

**Files:** `CLAUDE.md`, `README.md`, `ROADMAP.md`, `SECURITY.md`

- [ ] **Step 1:** docs. CLAUDE.md: pages/catalog module map (pages→never-imports-api direction as a non-negotiable; catalog.py = single source of catalog truth; autoescape + HTML-asymmetry as invariants; cookie = same auth_sessions rows, header-wins). README: "The storefront" section (routes, cookie login, phase-2 scope note + phase-3 pointer), commands unchanged. ROADMAP fork section: phase 2 done, phase 3 next (transact pages: booking flow, dashboards, offer inbox, payment + token pages, CSRF tokens for money forms). SECURITY.md: cookie posture (HttpOnly/Lax/Secure-flag semantics, raw-token-in-cookie = browser-held bearer equivalence, sha256-at-rest unchanged, logout/reset revocation applies to cookies identically), CSRF stance (Lax + POST-only, tokens deferred to phase-3 money forms), XSS boundary (autoescape + regression test), unauthenticated page surface additions. Doc-contradiction sweep: grep all four for "Swagger", "API-only", "frontend", "cookie", "session", "browse" and reconcile hit-by-hit.
- [ ] **Step 2:** full gates: ruff+format, pyright, full SQLite, full fork-PG, demo (24 steps, unchanged), live_server smoke already in suite. Fresh-chain NOT needed (no migration this branch — verify none was added).
- [ ] **Step 3:** commit ("Document the storefront: phase 2 pages foundation").

---

## Self-review notes (already applied)

- Spec §1-§2 → Task 1; §4 → Tasks 1-2; §3 → Task 3; §5 invariant → Task 1; docs → Task 4. Non-goals respected: NO booking form in any template (asserted by test), no CSRF tokens yet, no admin pages.
- The import-direction rule (pages never imports api) is enforced structurally by the catalog.py extraction — and stated as a Task-4 CLAUDE.md invariant so phase 3 doesn't regress it.
- `optional_claims` exists so public pages never 401 — distinct from `_principal`; both share `_resolve_bearer`.
- Cookie carries the raw token by design (browser-held bearer): revocation/TTL semantics are identical to API sessions; stated in SECURITY.md task.
- Type consistency: `session_token` cookie name identical in auth.py/pages.py/tests; `catalog.py` function names (`listing_bookable`, `visible_listings_stmt`, `indicative_buyer_price`) consistent across Tasks 1-2 and api.py imports.
- Line anchors verified 2026-07-17; match on snippets if drifted.
