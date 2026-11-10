"""Notification preferences: which kinds a user has muted. Must-send kinds
can never be muted, and a user only sees and sets their own role's kinds."""

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import notifications
from ..entities import NotificationMute, User
from ..errors import ConflictError, UnprocessableError
from ..models import NotificationPreferenceOut, NotificationPreferencesUpdate, UserRole


def _pref_rows(
    session: Session, user_id: str, user_role: UserRole
) -> list[NotificationPreferenceOut]:
    kinds = [k for k, role in notifications.KIND_ROLES.items() if role is user_role]
    muted = set(
        session.scalars(
            select(NotificationMute.kind).where(NotificationMute.user_id == user_id)
        ).all()
    )
    return [
        NotificationPreferenceOut(
            kind=k,
            muted=k in muted and k not in notifications.MUST_SEND,
            locked=k in notifications.MUST_SEND,
        )
        for k in kinds
    ]


def get_notification_preferences(
    session: Session, user_id: str, user_role: UserRole
) -> list[NotificationPreferenceOut]:
    return _pref_rows(session, user_id, user_role)


def put_notification_preferences(
    session: Session, user_id: str, user_role: UserRole, body: NotificationPreferencesUpdate
) -> list[NotificationPreferenceOut]:
    wanted = set(body.muted)
    for kind in wanted:
        if kind in notifications.MUST_SEND:
            raise UnprocessableError(f"{kind} cannot be muted")
        if notifications.KIND_ROLES[kind] is not user_role:
            raise UnprocessableError(f"{kind} is not a {user_role} kind")
    # Serialize concurrent PUTs for the same user: naive delete-then-insert
    # under READ COMMITTED unions both sets (each DELETE misses the other's
    # uncommitted rows). The row lock makes replace-set last-writer-wins.
    session.get(User, user_id, with_for_update=True)
    session.execute(delete(NotificationMute).where(NotificationMute.user_id == user_id))
    for kind in sorted(wanted):
        session.add(NotificationMute(user_id=user_id, kind=kind))
    try:
        session.flush()
    except IntegrityError:
        raise ConflictError("preferences changed concurrently, retry") from None
    return _pref_rows(session, user_id, user_role)
