"""Shared fixtures.

Tests run against a throwaway SQLite database (no Docker needed). We point
DATABASE_URL at a temp file *before* importing the app so the whole stack binds
to it, create the schema once, and delete all rows between tests. Set
DATABASE_URL yourself (e.g. a Postgres URL) to run the same suite against Postgres.
"""

import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{tempfile.mkdtemp()}/test.db")
# Real env vars outrank .env in pydantic-settings: pin these empty so a
# developer's .env (with a real Stripe key) can never flip the suite from the
# fake provider onto the live API.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")
os.environ.setdefault("SMTP_HOST", "")  # the suite must never send real mail

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import email_validator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from marketplace import api
from marketplace.auth import _hash_token, hash_password  # pyright: ignore[reportPrivateUsage]
from marketplace.db import SessionLocal, engine, init_db
from marketplace.entities import AuthSession, Base, SellerProfile, User
from marketplace.mail import RecordingEmailSender, use_sender
from marketplace.models import UserRole
from marketplace.payments import fake_provider
from marketplace.payments.fake import FakeProvider

# Test fixtures use reserved *.test addresses (RFC 2606); email-validator's
# EmailStr backing rejects them as "special-use" domains unless told this is a
# test environment. This is the library's own documented switch for it.
email_validator.TEST_ENVIRONMENT = True

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
def mail_outbox() -> Iterator[RecordingEmailSender]:
    """Capture outbound mail via the port (tests read tokens here, not logs)."""
    recorder = RecordingEmailSender()
    previous = use_sender(recorder)
    yield recorder
    use_sender(previous)


TEST_PASSWORD = "test-password-123"
_TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)  # hash once; argon2 is deliberately slow


@pytest.fixture
def auth() -> AuthFactory:
    """Bearer-header factory with the historical interface: auth(role, sub).

    White-box: inserts a User (id == sub, so identity assertions in older
    tests keep working) plus an AuthSession row. Idempotent per (role, sub)
    within a test — repeated calls return the same header."""
    issued: dict[tuple[str, str], Header] = {}

    def _make(role: str, sub: str) -> Header:
        key = (role, sub)
        if key in issued:
            return issued[key]
        raw = f"test-token-{role}-{sub}"
        with SessionLocal() as s:
            existing = s.get(User, sub)
            if existing is None:
                s.add(
                    User(
                        id=sub,
                        email=f"{sub}@{role}.test.local",
                        role=UserRole(role),
                        password_hash=_TEST_PASSWORD_HASH,
                        display_name=sub,
                    )
                )
                s.flush()  # persist the user before its FK-child AuthSession
                # (Postgres enforces the FK; SQLite doesn't, which hid this)
            else:
                # Same sub under a different role would silently authenticate
                # as the original role — fail loudly instead.
                assert existing.role == UserRole(role), (
                    f"fixture sub {sub!r} already exists with role "
                    f"{existing.role}, requested {role}"
                )
            s.add(
                AuthSession(
                    user_id=sub,
                    token_hash=_hash_token(raw),
                    expires_at=datetime.now(UTC) + timedelta(hours=12),
                )
            )
            s.commit()
        issued[key] = {"Authorization": f"Bearer {raw}"}
        return issued[key]

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
