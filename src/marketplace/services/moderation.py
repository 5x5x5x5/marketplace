"""Moderation: participant reports, the admin report queue, and user
suspension, reinstatement and display-name resets."""

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import notifications
from ..entities import Job, Report, Review, SellerReview, User
from ..errors import ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from ..models import (
    EventKind,
    ReportRequest,
    ReportStatus,
    ReportTargetKind,
    ResolveReportRequest,
    SuspendRequest,
    UserRole,
    UserStatus,
)
from ..repo import audit
from .common import now as _now
from .common import paginate as _paginate
from .common import require_active as _require_active


def file_report(session: Session, reporter_id: str, body: ReportRequest) -> Report:
    _require_active(session, reporter_id)
    target_id = body.target_id
    if body.target_kind is ReportTargetKind.USER:
        target = session.get(User, body.target_id)
        if target is None:
            raise NotFoundError("target not found")
        if target.id == reporter_id:
            raise UnprocessableError("cannot report yourself")
        shared = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                or_(
                    and_(Job.buyer_id == reporter_id, Job.seller_id == target.id),
                    and_(Job.seller_id == reporter_id, Job.buyer_id == target.id),
                )
            )
        )
        if not shared:
            raise ForbiddenError("not a counterparty")
    else:
        model = Review if body.target_kind is ReportTargetKind.REVIEW else SellerReview
        try:
            review_uuid = UUID(body.target_id)
        except ValueError:
            raise NotFoundError("target not found") from None
        row = session.get(model, review_uuid)
        if row is None:
            raise NotFoundError("target not found")
        if reporter_id not in (row.buyer_id, row.seller_id):
            raise ForbiddenError("not a party to this review")
        # Canonicalize: UUID() accepts many textual forms of the same value
        # (case, hyphens, braces, urn:uuid:); storing the raw string would let
        # each variant evade the (reporter, target) UNIQUE and re-notify admins.
        target_id = str(review_uuid)

    report = Report(
        reporter_id=reporter_id,
        target_kind=body.target_kind,
        target_id=target_id,
        reason=body.reason,
    )
    session.add(report)
    try:
        session.flush()
    except IntegrityError:
        # Duplicate (sequential or concurrent) loses the UNIQUE race — same
        # answer either way, not a 500.
        raise ConflictError("already reported") from None
    notifications.enqueue_admins(
        session,
        EventKind.REPORT_OPENED_ADMIN,
        {
            "report_id": str(report.id),
            "target_kind": body.target_kind,
            "target_id": target_id,
            "reason": body.reason,
            "reporter_id": reporter_id,
        },
    )
    return report


def my_reports(session: Session, reporter_id: str) -> list[Report]:
    return list(
        session.scalars(
            select(Report)
            .where(Report.reporter_id == reporter_id)
            .order_by(Report.created_at.desc())
        ).all()
    )


def suspend_user(session: Session, admin_id: str, user_id: str, body: SuspendRequest) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("user not found")
    if user.role is UserRole.ADMIN:
        raise UnprocessableError("admins cannot be suspended")
    if user.status is UserStatus.SUSPENDED:
        raise ConflictError("user already suspended")
    user.status = UserStatus.SUSPENDED
    user.suspended_reason = body.reason
    user.suspended_at = _now()
    audit(session, admin_id, "suspend_user", user_id, {"reason": body.reason})
    session.flush()
    return user


def reinstate_user(session: Session, admin_id: str, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("user not found")
    if user.status is not UserStatus.SUSPENDED:
        raise ConflictError("user is not suspended")
    user.status = UserStatus.ACTIVE
    user.suspended_reason = None
    user.suspended_at = None
    audit(session, admin_id, "reinstate_user", user_id, {})
    session.flush()
    return user


def admin_reset_display_name(session: Session, admin_id: str, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("user not found")
    user.display_name = f"user-{user.id[:8]}"
    audit(session, admin_id, "reset_display_name", user_id, {})
    session.flush()
    return user


def admin_list_reports(
    session: Session, status: ReportStatus | None, limit: int, offset: int
) -> list[Report]:
    q = select(Report).order_by(Report.created_at.desc(), Report.id.desc())
    if status is not None:
        q = q.where(Report.status == status)
    return _paginate(session, q, limit, offset)


def resolve_report(
    session: Session, admin_id: str, report_id: UUID, body: ResolveReportRequest
) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise NotFoundError("report not found")
    if report.status is not ReportStatus.OPEN:
        raise ConflictError("report already resolved")
    report.status = body.status
    report.resolution_note = body.note
    report.resolved_at = _now()
    audit(
        session,
        admin_id,
        "resolve_report",
        str(report_id),
        {"status": body.status, "note": body.note or ""},
    )
    session.flush()
    return report
