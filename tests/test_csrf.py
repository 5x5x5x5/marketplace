"""Session-bound CSRF tokens on authenticated page forms."""

import hashlib

from fastapi.testclient import TestClient

from marketplace.pages.csrf import CSRF_EXEMPT_POSTS, token_for
from tests.page_helpers import csrf_of, page_user


def test_token_is_domain_separated_from_the_stored_session_hash() -> None:
    raw = "some-raw-session-token"
    assert token_for(raw) != hashlib.sha256(raw.encode()).hexdigest()
    assert token_for(raw) != token_for(raw + "x")
    assert token_for(raw) == token_for(raw)  # deterministic: no server state


def test_rendered_token_matches_the_cookie_session() -> None:
    c = page_user("buyer")
    raw = c.cookies["session_token"]
    assert csrf_of(c) == token_for(raw)


def test_token_changes_with_each_login() -> None:
    a, b = page_user("buyer"), page_user("buyer")
    assert csrf_of(a) != csrf_of(b)


def test_logout_without_token_is_refused_and_the_session_survives() -> None:
    c = page_user("buyer")
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 403
    assert c.get("/v1/auth/me").status_code == 200


def test_logout_with_wrong_token_is_refused() -> None:
    c = page_user("buyer")
    other = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": csrf_of(other)}, follow_redirects=False)
    assert r.status_code == 403
    assert c.get("/v1/auth/me").status_code == 200


def test_logout_with_token_works(client: TestClient) -> None:
    c = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": csrf_of(c)}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/v1/auth/me").status_code == 401


def test_non_ascii_token_fails_closed_not_500() -> None:
    c = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": "é" * 64}, follow_redirects=False)
    assert r.status_code == 403


def test_exempt_list_is_exactly_the_unauthenticated_forms() -> None:
    assert {
        "/signup",
        "/login",
        "/verify",
        "/password-reset/request",
        "/password-reset/confirm",
    } == CSRF_EXEMPT_POSTS
