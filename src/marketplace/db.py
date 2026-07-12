"""Database engine + session dependency.

`get_session` is the FastAPI dependency every endpoint uses: one session per
request, committed on success and rolled back on any exception (including
HTTPException, so a 4xx never persists partial work).
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

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


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables. Used for SQLite/dev; production uses Alembic migrations."""
    Base.metadata.create_all(engine)
