"""Helpers for driving the server-rendered pages the way a browser does: one
TestClient (one cookie jar) per user, CSRF tokens read from rendered HTML."""

import re
from uuid import uuid4

from fastapi.testclient import TestClient

from marketplace import api
from tests.conftest import TEST_PASSWORD

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def page_user(role: str, name: str = "Pat", email: str | None = None) -> TestClient:
    """Sign up through the page; the returned client carries the session cookie."""
    c = TestClient(api.app)
    r = c.post(
        "/signup",
        data={
            "email": email or f"{role}-{uuid4().hex[:8]}@example.test",
            "password": TEST_PASSWORD,
            "role": role,
            "display_name": name,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return c


def csrf_from(html: str) -> str:
    m = _CSRF.search(html)
    assert m is not None, "no csrf_token field in the page"
    return m.group(1)


def csrf_of(c: TestClient, path: str = "/") -> str:
    return csrf_from(c.get(path).text)


def user_id(c: TestClient) -> str:
    r = c.get("/v1/auth/me")
    assert r.status_code == 200, r.text
    return str(r.json()["id"])
