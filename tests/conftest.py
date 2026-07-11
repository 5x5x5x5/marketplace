"""Shared fixtures.

The app keeps a module-level Config and Store. We reset both before each test
and provide a TestClient bound to the app. Every request now needs a bearer
token, so `auth(role, sub)` mints the header and `admin` is the operator header.
"""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from marketplace import api
from marketplace.auth import mint_token

Header = dict[str, str]
AuthFactory = Callable[[str, str], Header]


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    api.reset_state()
    yield
    api.reset_state()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


@pytest.fixture
def auth() -> AuthFactory:
    def _make(role: str, sub: str) -> Header:
        return {"Authorization": f"Bearer {mint_token(role, sub)}"}

    return _make


@pytest.fixture
def admin(auth: AuthFactory) -> Header:
    return auth("admin", "ops")


@pytest.fixture
def basic_service(client: TestClient, admin: Header) -> str:
    """Configure a default service type and pipelines, return its id."""
    sid = "rideshare"
    r = client.put(
        f"/admin/config/service_types/{sid}",
        json={"base_buyer_price": 20.0, "base_seller_payout": 14.0},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{sid}",
        json={"buyer": [], "seller": []},
        headers=admin,
    )
    assert r.status_code == 200
    return sid
