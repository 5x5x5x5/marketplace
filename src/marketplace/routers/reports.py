"""Participant reports (`/v1/reports`): any buyer or seller files one and
lists their own. Logic lives in `services.moderation`."""

from fastapi import APIRouter

from ..db import CommitRoute
from ..entities import Report
from ..models import ReportOut, ReportRequest
from ..services import moderation
from .deps import ParticipantClaims, SessionDep

reports_router = APIRouter(prefix="/v1", tags=["reports"], route_class=CommitRoute)


@reports_router.post("/reports", response_model=ReportOut, status_code=201)
def file_report(body: ReportRequest, session: SessionDep, claims: ParticipantClaims) -> Report:
    return moderation.file_report(session, claims.sub, body)


@reports_router.get("/reports", response_model=list[ReportOut])
def my_reports(session: SessionDep, claims: ParticipantClaims) -> list[Report]:
    return moderation.my_reports(session, claims.sub)
