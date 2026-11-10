"""Capacity: connection-pool sizing and load shedding on pool exhaustion."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from marketplace import api, db, idempotency, pages, repo
from marketplace.db import SessionLocal, engine, engine_options
from marketplace.entities import Availability, Job, Listing, Offer, Quote, SellerProfile
from marketplace.models import JobStatus, OfferStatus
from marketplace.services import booking, maintenance
from marketplace.settings import Settings
from tests.conftest import AuthFactory, Header
from tests.test_migrations import _alembic  # pyright: ignore[reportPrivateUsage]

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

    monkeypatch.setattr(pages.storefront, "PAGE_SIZE", 4)
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


# ---------- quote path ----------


def _insert_sellers(
    auth: AuthFactory, service_type_id: str, prefix: str, n: int, *, ready: bool = True
) -> list[str]:
    ids = [f"{prefix}{i}" for i in range(n)]
    for sid in ids:
        auth("seller", sid)
    with SessionLocal() as s:
        for sid in ids:
            s.add(SellerProfile(id=sid, payments_ready=ready, provider_account_id=f"acct_{sid}"))
            s.add(Availability(seller_id=sid, service_type_id=service_type_id))
        s.commit()
    return ids


def _candidate_queries(service_type_id: str) -> tuple[int, list[str]]:
    with SessionLocal() as s, record_queries() as log:
        cands = repo.eligible_candidates(s, service_type_id, set())
    return len(log.statements), [c.seller_id for c in cands]


def test_candidate_load_does_not_grow_with_sellers(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _insert_sellers(auth, basic_service, "few", 2)
    few_queries, few = _candidate_queries(basic_service)
    assert len(few) == 2
    _insert_sellers(auth, basic_service, "many", 25)
    many_queries, many = _candidate_queries(basic_service)
    assert len(many) == 27
    assert many_queries <= few_queries


def test_candidate_load_filters_like_before(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    ok, busy, gone, excluded = _insert_sellers(auth, basic_service, "c", 4)
    (unready,) = _insert_sellers(auth, basic_service, "unready", 1, ready=False)
    with SessionLocal() as s:
        s.add(
            Job(
                quote_id=uuid4(),
                buyer_id="b",
                service_type_id=basic_service,
                buyer_price=Decimal("20.00"),
                seller_id=busy,
                status=JobStatus.AWAITING_PAYMENT,  # holds busy's only slot
            )
        )
        s.commit()
    assert (
        client.post(
            f"/v1/admin/users/{gone}/suspend", json={"reason": "test"}, headers=admin
        ).status_code
        == 200
    )
    with SessionLocal() as s:
        cands = repo.eligible_candidates(s, basic_service, {excluded})
    assert [c.seller_id for c in cands] == [ok]
    assert unready not in [c.seller_id for c in cands]


def test_quote_query_count_does_not_grow_with_sellers(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    buyer = auth("buyer", "q-buyer")
    client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)  # warm
    _insert_sellers(auth, basic_service, "qa", 2)
    with record_queries() as few:
        r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 201
    _insert_sellers(auth, basic_service, "qb", 25)
    with record_queries() as many:
        r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 201
    assert len(many.statements) <= len(few.statements)


def test_quote_cap_counts_live_quotes_only(
    client: TestClient, basic_service: str, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(booking, "MAX_ACTIVE_QUOTES", 2)
    past = datetime.now(UTC) - timedelta(hours=1)
    with SessionLocal() as s:
        for _ in range(3):
            s.add(
                Quote(
                    buyer_id="x",
                    service_type_id=basic_service,
                    buyer_price=Decimal("1.00"),
                    expires_at=past,
                )
            )
        s.commit()
    buyer = auth("buyer", "capper")
    body = {"service_type_id": basic_service}
    assert client.post("/v1/quotes", json=body, headers=buyer).status_code == 201
    assert client.post("/v1/quotes", json=body, headers=buyer).status_code == 201
    assert client.post("/v1/quotes", json=body, headers=buyer).status_code == 503


def test_expired_quotes_are_reaped_by_housekeeping(
    client: TestClient, basic_service: str, admin: Header
) -> None:
    with SessionLocal() as s:
        s.add(
            Quote(
                buyer_id="x",
                service_type_id=basic_service,
                buyer_price=Decimal("1.00"),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        s.commit()
    assert client.post("/v1/admin/jobs/sweep", headers=admin).status_code == 200
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Quote)) == 0


def test_migrations_create_hot_path_indexes(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'idx.db'}"
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        quotes = {i["name"]: i["column_names"] for i in insp.get_indexes("quotes")}
        jobs = {i["name"]: i["column_names"] for i in insp.get_indexes("jobs")}
    finally:
        eng.dispose()
    assert quotes["ix_quotes_expires_at"] == ["expires_at"]
    assert jobs["ix_jobs_service_type_id_status"] == ["service_type_id", "status"]
    assert jobs["ix_jobs_seller_id_status"] == ["seller_id", "status"]


# ---------- sweeps and the maintenance loop ----------


def test_read_path_sweep_skips_housekeeping(client: TestClient, auth: AuthFactory) -> None:
    seller = auth("seller", "poller")
    with record_queries() as log:
        assert client.get("/v1/seller/offers", headers=seller).status_code == 200
    deletes = [s for s in log.statements if s.lstrip().upper().startswith("DELETE")]
    assert deletes == []


def test_read_path_still_expires_offers_lazily(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """The lazy offer-expiry sweep is correctness, not housekeeping: a read
    must still walk an expired offer on to the next seller."""
    _insert_sellers(auth, basic_service, "lazy", 2)
    buyer = auth("buyer", "lazy-buyer")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    job = client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).json()
    with SessionLocal() as s:
        offer = s.scalar(select(Offer).where(Offer.job_id == UUID(job["id"])))
        assert offer is not None
        first = offer.seller_id
        offer.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    client.get("/v1/seller/offers", headers=auth("seller", first))
    with SessionLocal() as s:
        offers = s.scalars(select(Offer).where(Offer.job_id == UUID(job["id"]))).all()
    assert sorted(o.status for o in offers) == sorted([OfferStatus.EXPIRED, OfferStatus.OFFERED])


def _lifespan_starts_loop(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> bool:
    started: list[bool] = []

    async def fake_loop() -> None:
        started.append(True)

    monkeypatch.setattr(maintenance, "maintenance_loop", fake_loop)
    monkeypatch.setattr(api.settings, "maintenance_enabled", enabled)
    with TestClient(api.app) as c:
        assert c.get("/healthz").status_code == 200
    return bool(started)


def test_maintenance_enabled_gates_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _lifespan_starts_loop(monkeypatch, enabled=True)
    assert not _lifespan_starts_loop(monkeypatch, enabled=False)


def test_maintenance_enabled_defaults_on() -> None:
    assert _settings().maintenance_enabled is True


def test_idempotency_db_work_runs_off_the_event_loop(
    client: TestClient, basic_service: str, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_loop: list[bool] = []
    real = idempotency.SessionLocal

    def tracking_session() -> Session:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real()

    monkeypatch.setattr(idempotency, "SessionLocal", tracking_session)
    headers = {**auth("buyer", "idem-buyer"), "Idempotency-Key": "k1"}
    body = {"service_type_id": basic_service}
    first = client.post("/v1/quotes", json=body, headers=headers)
    replay = client.post("/v1/quotes", json=body, headers=headers)
    assert first.status_code == 201 and replay.json() == first.json()
    assert on_loop and not any(on_loop)  # lookup + store + replay lookup, all in threads
