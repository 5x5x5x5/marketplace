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
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from .entities import Base
from .settings import settings


def _connect_args(url: str) -> dict[str, object]:
    # SQLite runs endpoint handlers across the threadpool; allow cross-thread use.
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args=_connect_args(settings.database_url),
)
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
