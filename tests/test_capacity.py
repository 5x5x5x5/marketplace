"""Capacity: connection-pool sizing and load shedding on pool exhaustion."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from marketplace import db
from marketplace.db import engine, engine_options
from marketplace.settings import Settings

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
