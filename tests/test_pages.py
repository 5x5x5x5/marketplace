"""Server-rendered pages: catalog grid, listing detail, seller profile,
shared-logic extraction, invariant."""

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from marketplace.db import SessionLocal
from marketplace.entities import SellerProfile
from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _listed_seller(
    client: TestClient, auth: AuthFactory, sid: str, title: str = "City rides", **over: object
) -> dict[str, Any]:
    onboard_and_avail(client, auth, sid, "s1")
    body: dict[str, object] = {
        "service_type_id": sid,
        "title": title,
        "description": "Vetted drivers",
    } | over
    r = client.post("/v1/seller/listings", json=body, headers=auth("seller", "s1"))
    assert r.status_code == 201, r.text
    return r.json()


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


def test_landing_hides_invisible(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _listed_seller(client, auth, basic_service)
    lid = client.get("/v1/seller/listings", headers=auth("seller", "s1")).json()[0]["id"]
    assert client.post(f"/v1/admin/listings/{lid}/hide", headers=admin).status_code == 200
    assert "City rides" not in client.get("/").text


def test_page_source_asymmetry(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    html = client.get("/").text.lower()
    for forbidden in ("seller_payout", "margin", "capacity", "payments_ready", "@"):
        assert forbidden not in html, forbidden


def test_xss_listing_title_escaped(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _listed_seller(client, auth, basic_service, title="<script>alert(1)</script>")
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_static_served(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_listing_detail_renders(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    listing = _listed_seller(
        client, auth, basic_service, photo_urls=["https://img.test.local/a.jpg"]
    )
    r = client.get(f"/listings/{listing['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "City rides" in r.text
    assert "Vetted drivers" in r.text
    assert 'src="https://img.test.local/a.jpg"' in r.text
    assert "20.00" in r.text
    assert 'href="/sellers/s1"' in r.text


def test_listing_detail_no_photos_shows_placeholder(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    listing = _listed_seller(client, auth, basic_service)
    r = client.get(f"/listings/{listing['id']}")
    assert r.status_code == 200
    assert "listing-photo-placeholder" in r.text
    assert "<img" not in r.text


def test_listing_detail_unknown_404(client: TestClient) -> None:
    r = client.get(f"/listings/{uuid4()}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_listing_detail_hidden_404(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    listing = _listed_seller(client, auth, basic_service)
    assert client.post(f"/v1/admin/listings/{listing['id']}/hide", headers=admin).status_code == 200
    r = client.get(f"/listings/{listing['id']}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_listing_detail_anonymous_no_booking_form(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    listing = _listed_seller(client, auth, basic_service)
    html = client.get(f"/listings/{listing['id']}").text
    assert 'href="/login"' in html
    assert "/v1/quotes" not in html
    assert "/v1/jobs" not in html
    assert "<form" not in html.lower()


def test_seller_profile_renders(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    r = client.get("/sellers/s1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "City rides" in r.text
    assert "No ratings" in r.text


def test_seller_profile_suspended_404(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _listed_seller(client, auth, basic_service)
    assert (
        client.post(
            "/v1/admin/users/s1/suspend", json={"reason": "test"}, headers=admin
        ).status_code
        == 200
    )
    r = client.get("/sellers/s1")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_seller_profile_unknown_404(client: TestClient) -> None:
    r = client.get("/sellers/nope")
    assert r.status_code == 404


def test_seller_profile_not_onboarded_404(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _listed_seller(client, auth, basic_service)
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None
        prof.payments_ready = False
        s.commit()
    r = client.get("/sellers/s1")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_page_source_asymmetry_detail_and_seller(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    listing = _listed_seller(client, auth, basic_service)
    for url in (f"/listings/{listing['id']}", "/sellers/s1"):
        html = client.get(url).text.lower()
        for forbidden in ("seller_payout", "margin", "capacity", "payments_ready", "@"):
            assert forbidden not in html, (url, forbidden)


def test_xss_listing_title_escaped_detail_and_seller(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    listing = _listed_seller(
        client,
        auth,
        basic_service,
        title="<script>alert(1)</script>",
        photo_urls=["https://img.test.local/xss.jpg"],
    )
    for url in (f"/listings/{listing['id']}", "/sellers/s1"):
        html = client.get(url).text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    # The img alt attribute is a separate sink from the page body text above
    # (attribute context, not element content) — exercise it directly by
    # isolating the <img ...> tag and checking escaping within it.
    detail_html = client.get(f"/listings/{listing['id']}").text
    img_start = detail_html.index("<img")
    img_end = detail_html.index("/>", img_start)
    img_tag = detail_html[img_start:img_end]
    assert "<script>alert(1)</script>" not in img_tag
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in img_tag


def test_signup_page_renders_role_picker(client: TestClient) -> None:
    html = client.get("/signup").text
    assert 'name="role"' in html
    assert 'value="buyer"' in html
    assert 'value="seller"' in html
    assert "<form" in html.lower()


def test_login_page_renders(client: TestClient) -> None:
    html = client.get("/login").text
    assert 'action="/login"' in html
    assert 'name="password"' in html


def test_nav_shows_login_signup_links_when_anonymous(client: TestClient) -> None:
    html = client.get("/").text
    assert 'href="/login"' in html
    assert 'href="/signup"' in html
    assert "Log out" not in html


def test_nav_shows_display_name_and_logout_when_cookied(client: TestClient) -> None:
    from tests.conftest import TEST_PASSWORD

    r = client.post(
        "/signup",
        data={
            "email": "navuser@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Nav User",
        },
    )
    assert r.status_code == 200  # followed the 303 redirect to "/"
    html = r.text
    assert "Nav User" in html
    assert "buyer" in html
    assert "Log out" in html
    assert 'href="/signup"' not in html
    assert 'href="/login"' not in html


def test_login_page_rejects_admin_role(client: TestClient, monkeypatch: Any) -> None:
    # The login FORM only offers buyer/seller, but the POST target must not
    # forward a wider role to auth.login than the form advertises — even
    # with the true admin password, role=admin must not reach an admin
    # session through this page surface. Uses the real bootstrap_admin path
    # (not the `admin` fixture) so this is unambiguously the true password.
    from marketplace import auth as auth_module
    from marketplace.settings import settings as app_settings

    monkeypatch.setattr(app_settings, "admin_email", "root@example.test")
    monkeypatch.setattr(app_settings, "admin_password", "bootstrap-password-1")
    auth_module.bootstrap_admin()

    r = client.post(
        "/login",
        data={"email": "root@example.test", "password": "bootstrap-password-1", "role": "admin"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "session_token" not in r.cookies
    # Same generic copy as a wrong-password attempt — no signal that "admin"
    # was the problem (enumeration-safe).
    assert "Invalid email or password." in r.text


def _signup_cookie(client: TestClient, email: str, name: str = "Nav User") -> dict[str, str]:
    from tests.conftest import TEST_PASSWORD

    r = client.post(
        "/signup",
        data={"email": email, "password": TEST_PASSWORD, "role": "buyer", "display_name": name},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return {"session_token": r.cookies["session_token"]}


def test_404_page_shows_logged_in_nav_when_cookied(client: TestClient) -> None:
    """m3: pages.py:107 hardcoded claims=None on the 404 context — a
    logged-in user hitting a dead listing/seller link would see logged-out
    nav. The real nav context must come from the resolved cookie."""
    cookies = _signup_cookie(client, "nav-404@example.test")
    r = client.get(f"/listings/{uuid4()}", cookies=cookies)
    assert r.status_code == 404
    assert "Nav User" in r.text
    assert "Log out" in r.text
    assert 'href="/login"' not in r.text


def test_signup_page_shows_logged_in_nav_when_cookied(client: TestClient) -> None:
    """m3: pages.py:161 hardcoded claims=None on GET /signup. Logged-in-nav
    while already logged in is correct; the form itself still renders."""
    cookies = _signup_cookie(client, "nav-signup@example.test")
    r = client.get("/signup", cookies=cookies)
    assert r.status_code == 200
    assert "Nav User" in r.text
    assert "Log out" in r.text
    assert "<form" in r.text.lower()  # the signup form still renders


def test_login_page_shows_logged_in_nav_when_cookied(client: TestClient) -> None:
    """m3: pages.py:168 hardcoded claims=None on GET /login. Logged-in-nav
    while already logged in is correct; the form itself still renders."""
    cookies = _signup_cookie(client, "nav-login@example.test")
    r = client.get("/login", cookies=cookies)
    assert r.status_code == 200
    assert "Nav User" in r.text
    assert "Log out" in r.text
    assert "<form" in r.text.lower()  # the login form still renders


def test_commit_route_invariant_covers_pages() -> None:
    import pytest
    from fastapi import APIRouter, FastAPI

    from marketplace import api

    rogue_app = FastAPI()
    rogue = APIRouter()  # NOT /v1 — the widened invariant must still catch it

    @rogue.get("/rogue-page")
    def rogue_page() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    rogue_app.include_router(rogue)
    with pytest.raises(RuntimeError, match="CommitRoute"):
        api._assert_commit_routes(rogue_app)  # pyright: ignore[reportPrivateUsage]
