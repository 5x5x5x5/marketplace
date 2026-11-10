"""Notification preferences (`/v1/notification-preferences`) for the
authenticated user. Logic lives in `services.preferences`."""

from fastapi import APIRouter

from ..auth import Principal
from ..db import CommitRoute
from ..models import NotificationPreferenceOut, NotificationPreferencesUpdate
from ..services import preferences
from .deps import SessionDep

prefs_router = APIRouter(prefix="/v1", tags=["preferences"], route_class=CommitRoute)


@prefs_router.get("/notification-preferences", response_model=list[NotificationPreferenceOut])
def get_notification_preferences(
    session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return preferences.get_notification_preferences(session, claims.sub, claims.role)


@prefs_router.put("/notification-preferences", response_model=list[NotificationPreferenceOut])
def put_notification_preferences(
    body: NotificationPreferencesUpdate, session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return preferences.put_notification_preferences(session, claims.sub, claims.role, body)
