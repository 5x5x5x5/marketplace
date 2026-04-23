"""Shared fixtures.

The app keeps a module-level Config and Store. We reset both before each test
and provide a TestClient bound to the app.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from marketplace import api


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    api.reset_state()
    yield
    api.reset_state()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


@pytest.fixture
def basic_service(client: TestClient) -> str:
    """Configure a default service type and pipelines, return its id."""
    sid = "rideshare"
    r = client.put(
        f"/admin/config/service_types/{sid}",
        json={"base_buyer_price": 20.0, "base_seller_payout": 14.0},
    )
    assert r.status_code == 200
    r = client.put(
        f"/admin/config/pipelines/{sid}",
        json={"buyer": [], "seller": []},
    )
    assert r.status_code == 200
    return sid
