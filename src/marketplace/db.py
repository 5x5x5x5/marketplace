"""Database engine + session dependency.

`get_session` is the FastAPI dependency every endpoint uses: one session per
request. Unlike a plain `APIRoute`, `CommitRoute` (below) commits the session
*before* the response is handed back to the sender — never in dependency
teardown, which FastAPI runs after the response is already on the wire
(finding F2). A 4xx/5xx never persists work since the last handler-owned
commit (the dispute-resolve pin deliberately commits mid-handler):
`CommitRoute` only commits on responses under 400, and `get_session`'s
teardown always closes the session, which discards (rolls back) anything
left uncommitted.
"""

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from .entities import Base
from .settings import Settings, settings


def _connect_args(url: str) -> dict[str, object]:
    # SQLite runs endpoint handlers across the threadpool; allow cross-thread use.
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


def engine_options(cfg: Settings) -> dict[str, Any]:
    """Pool arguments for `create_engine`, sized on purpose rather than left at
    SQLAlchemy's 5+10 / 30 s defaults (which, against Starlette's 40-token
    threadpool, collapsed into 30 s timeouts at ~100 concurrent requests).

    SQLite keeps SQLAlchemy's own pool choice untouched: it is the local/test
    backend and has no server-side connection cap to size against. `DB_POOL=
    null` opens a fresh connection per checkout, for deployments behind an
    external pooler (PgBouncer and friends) that already owns pooling."""
    opts: dict[str, Any] = {"connect_args": _connect_args(cfg.database_url)}
    if cfg.database_url.startswith("sqlite"):
        opts["pool_pre_ping"] = True
        return opts
    if cfg.db_pool == "null":
        opts["poolclass"] = NullPool
        return opts
    opts.update(
        pool_pre_ping=True,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_timeout=cfg.db_pool_timeout_seconds,
    )
    return opts


engine = create_engine(settings.database_url, **engine_options(settings))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    session = SessionLocal()
    request.state.db_session = session
    try:
        yield session
    finally:
        session.close()  # close() discards (rolls back) anything uncommitted


class CommitRoute(APIRoute):
    """Commits the request's DB session BEFORE the response object reaches
    the sender — never in dependency teardown, which runs after the response
    is on the wire (finding F2). Streaming responses would need a rethink;
    the app has none."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def commit_then_respond(request: Request) -> Response:
            response = await original(request)
            session: Session | None = getattr(request.state, "db_session", None)
            if session is not None and response.status_code < 400:
                # threadpool: a sync commit on the event loop would stall
                # every in-flight request (same class as the webhook offload)
                await run_in_threadpool(session.commit)
            return response

        return commit_then_respond


def init_db() -> None:
    """Create all tables. Used for SQLite/dev; production uses Alembic migrations."""
    Base.metadata.create_all(engine)
