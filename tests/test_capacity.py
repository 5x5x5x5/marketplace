"""Capacity: connection-pool sizing and load shedding on pool exhaustion."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from marketplace import db, pages
from marketplace.db import SessionLocal, engine, engine_options
from marketplace.entities import Listing
from marketplace.settings import Settings
from tests.conftest import AuthFactory

PG_URL = "postgresql+psycopg://u:p@localhost:5432/x"  # never connected to


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def test_postgres_pool_defaults_are_sized_not_sqlalchemy_defaults() -> None:
    eng = create_engine(PG_URL, **engine_options(_settings(database_url=PG_URL)))
    pool = eng.pool
    assert isinstance(pool, QueuePool)
    # 10 + 30 = 40: one connection per default threadpool token, not 5 + 10.
    assert pool.size() == 10
    assert pool._max_overflow == 30  # pyright: ignore[reportPrivateUsage]
    assert pool._timeout == 5.0  # pyright: ignore[reportPrivateUsage]


def test_pool_settings_are_configurable() -> None:
    cfg = _settings(
        database_url=PG_URL, db_pool_size=3, db_max_overflow=4, db_pool_timeout_seconds=1.5
    )
    pool = create_engine(PG_URL, **engine_options(cfg)).pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == 3
    assert pool._max_overflow == 4  # pyright: ignore[reportPrivateUsage]
    assert pool._timeout == 1.5  # pyright: ignore[reportPrivateUsage]


def test_null_pool_mode_for_external_pooler() -> None:
    cfg = _settings(database_url=PG_URL, db_pool="null")
    assert isinstance(create_engine(PG_URL, **engine_options(cfg)).pool, NullPool)


def test_sqlite_ignores_pool_settings() -> None:
    url = "sqlite+pysqlite:///./never-created.db"
    opts = engine_options(_settings(database_url=url, db_pool_size=99, db_pool="null"))
    assert "pool_size" not in opts and "poolclass" not in opts
    assert opts["connect_args"] == {"check_same_thread": False}


@pytest.fixture
def exhausted_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Swap request sessions onto a one-connection pool whose only connection
    is already checked out, with a near-zero checkout timeout."""
    tiny = create_engine(
        engine.url,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
        connect_args=db._connect_args(str(engine.url)),  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=tiny, expire_on_commit=False))
    held = tiny.connect()
    try:
        yield tiny
    finally:
        held.close()
        tiny.dispose()


def test_pool_exhaustion_sheds_with_503_not_500(client: TestClient, exhausted_pool: Engine) -> None:
    r = client.get("/v1/catalog/listings")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json()["detail"] == "server busy, retry shortly"
    assert "x-request-id" in r.headers


# ---------- query-shape helpers ----------


class QueryLog:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def touching(self, table: str) -> list[str]:
        return [s for s in self.statements if f"FROM {table}" in s]


@contextmanager
def record_queries() -> Iterator[QueryLog]:
    log = QueryLog()

    def _before(
        conn: object, cursor: object, statement: str, params: object, ctx: object, many: bool
    ) -> None:
        log.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield log
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _onboarded_seller(client: TestClient, auth: AuthFactory, sid: str) -> str:
    r = client.post("/v1/seller/payments/onboard", headers=auth("seller", sid))
    assert r.status_code == 200
    return sid


def _insert_listings(seller_id: str, service_type_id: str, n: int, at: datetime) -> list[str]:
    with SessionLocal() as s:
        rows = [
            Listing(
                seller_id=seller_id,
                service_type_id=service_type_id,
                title=f"listing {i}",
                created_at=at,
            )
            for i in range(n)
        ]
        s.add_all(rows)
        s.commit()
        return [str(r.id) for r in rows]


# ---------- catalog pagination ----------


def test_catalog_page_is_bounded_in_sql(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _onboarded_seller(client, auth, "cat-seller")
    _insert_listings(seller, basic_service, 3, datetime.now(UTC))
    with record_queries() as small:
        r = client.get("/v1/catalog/listings", params={"limit": 2})
    assert r.status_code == 200 and len(r.json()) == 2
    listing_reads = small.touching("listings")
    assert listing_reads and all("LIMIT" in s for s in listing_reads)

    _insert_listings(seller, basic_service, 40, datetime.now(UTC))
    with record_queries() as big:
        r = client.get("/v1/catalog/listings", params={"limit": 2})
    assert r.status_code == 200 and len(r.json()) == 2
    assert len(big.statements) <= len(small.statements)  # does not grow with N


def test_catalog_pages_are_stable_under_created_at_ties(
    client: TestClient, basic_service: str, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seller = _onboarded_seller(client, auth, "tie-seller")
    ids = _insert_listings(seller, basic_service, 7, datetime(2026, 1, 1, tzinfo=UTC))
    seen: list[str] = []
    for offset in (0, 3, 6):
        r = client.get("/v1/catalog/listings", params={"limit": 3, "offset": offset})
        assert r.status_code == 200
        seen += [row["id"] for row in r.json()]
    assert sorted(seen) == sorted(ids)  # no listing skipped or repeated

    monkeypatch.setattr(pages, "_PAGE_SIZE", 4)
    titles = [f">listing {i}<" for i in range(7)]
    shown: list[str] = []
    for page in (1, 2):
        html = client.get("/", params={"page": page}).text
        shown += [t for t in titles if t in html]
    assert sorted(shown) == sorted(titles)  # landing page pages the same way


@pytest.mark.parametrize(
    ("path", "role", "table"),
    [
        ("/v1/jobs", "buyer", "jobs"),
        ("/v1/seller/offers", "seller", "offers"),
        ("/v1/seller/jobs", "seller", "jobs"),
        ("/v1/admin/jobs", "admin", "jobs"),
        ("/v1/admin/audit", "admin", "audit_log"),
        ("/v1/admin/notifications", "admin", "notifications"),
        ("/v1/admin/transactions", "admin", "transactions"),
        ("/v1/admin/payouts", "admin", "payouts"),
        ("/v1/admin/disputes", "admin", "disputes"),
        ("/v1/admin/reports", "admin", "reports"),
        ("/v1/admin/buyers", "admin", "buyer_profiles"),
        ("/v1/admin/listings", "admin", "listings"),
        ("/v1/admin/reviews/buyer", "admin", "reviews"),
        ("/v1/admin/reviews/seller", "admin", "seller_reviews"),
    ],
)
def test_list_endpoints_paginate_in_sql(
    client: TestClient, auth: AuthFactory, path: str, role: str, table: str
) -> None:
    headers = auth(role, f"lister-{role}")
    with record_queries() as log:
        r = client.get(path, params={"limit": 5}, headers=headers)
    assert r.status_code == 200, r.text
    page_reads = [s for s in log.touching(table) if "count(" not in s.lower()]
    assert page_reads, log.statements
    assert any("LIMIT" in s for s in page_reads), page_reads
