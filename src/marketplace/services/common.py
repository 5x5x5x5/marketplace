"""Small helpers shared by every service module."""

from datetime import UTC, datetime

from sqlalchemy import Select
from sqlalchemy.orm import Session

from ..entities import User
from ..errors import EmailNotVerifiedError, ForbiddenError
from ..models import UserStatus
from ..settings import settings


def now() -> datetime:
    return datetime.now(UTC)


def require_active(session: Session, user_id: str) -> None:
    """Verb gate for suspension: acquisition endpoints call this first.
    A missing row is treated as active (auth already proved the principal)."""
    user = session.get(User, user_id)
    if user is not None and user.status is UserStatus.SUSPENDED:
        raise ForbiddenError("account suspended")


def require_verified_email(session: Session, user_id: str) -> None:
    """Verb gate for `REQUIRE_VERIFIED_EMAIL`: booking, accepting and listing
    call this after `require_active`, before any lock or write. A missing
    user row fails closed (unlike suspension, there is nothing to assume)."""
    if not settings.require_verified_email:
        return
    user = session.get(User, user_id)
    if user is None or not user.email_verified:
        raise EmailNotVerifiedError("verify your email address first")


def paginate[T](session: Session, stmt: Select[T], limit: int, offset: int) -> list[T]:
    """LIMIT/OFFSET in SQL: list endpoints never load a whole table to return
    one page. `stmt` must carry a total order (a unique tie-breaker) or rows
    can repeat/skip across pages."""
    return list(session.scalars(stmt.limit(limit).offset(offset)).all())
