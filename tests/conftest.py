"""Shared fixtures.

Tests run against a throwaway SQLite database (no Docker needed). We point
DATABASE_URL at a temp file *before* importing the app so the whole stack binds
to it, create the schema once, and delete all rows between tests. Set
DATABASE_URL yourself (e.g. a Postgres URL) to run the same suite against Postgres.
"""

import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("MARKETPLACE_SECRET", "test-secret")
# Real env vars outrank .env in pydantic-settings: pin these empty so a
# developer's .env (with a real Stripe key) can never flip the suite from the
# fake provider onto the live API.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from marketplace import api
from marketplace.auth import mint_token
from marketplace.db import SessionLocal, engine, init_db
from marketplace.entities import Base, SellerProfile
from marketplace.payments import fake_provider
from marketplace.payments.fake import FakeProvider

Header = dict[str, str]
AuthFactory = Callable[[str, str], Header]

init_db()
IS_POSTGRES = engine.url.get_backend_name() == "postgresql"


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(delete(table))


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


@pytest.fixture(autouse=True)
def _reset_fake_payments() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """The fake provider is a module singleton; scripted state must not leak."""
    fake_provider.reset()
    yield
    fake_provider.reset()


@pytest.fixture
def fake_payments() -> FakeProvider:
    """The live fake-provider singleton, for scripting statuses/outages."""
    return fake_provider


@pytest.fixture
def basic_service(client: TestClient, admin: Header) -> str:
    """Configure a default service type + empty pipelines; return its id."""
    sid = "rideshare"
    r = client.put(
        f"/v1/admin/config/service_types/{sid}",
        json={"base_buyer_price": 20, "base_seller_payout": 14},
        headers=admin,
    )
    assert r.status_code == 200
    r = client.put(
        f"/v1/admin/config/pipelines/{sid}", json={"buyer": [], "seller": []}, headers=admin
    )
    assert r.status_code == 200
    return sid


@pytest.fixture
def seed_rating() -> Callable[[str, int, int], None]:
    """White-box helper: set a seller's rating aggregates directly (ratings are
    otherwise only written by the review flow)."""

    def _seed(seller_id: str, rating_sum: int, rating_count: int) -> None:
        with SessionLocal() as s:
            prof = s.get(SellerProfile, seller_id) or SellerProfile(id=seller_id)
            prof.rating_sum = rating_sum
            prof.rating_count = rating_count
            s.add(prof)
            s.commit()

    return _seed
