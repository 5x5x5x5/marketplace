"""Reviews in both directions (buyer -> seller, seller -> buyer), their
role-scoped reads, and admin comment moderation."""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import repo
from ..entities import Job, Review, SellerReview
from ..errors import ConflictError, NotFoundError
from ..models import AdminReviewOut, JobReviewOut, JobStatus, ReportTargetKind, ReviewRequest
from ..repo import audit
from .common import paginate as _paginate
from .common import require_active as _require_active


def _job_reviews(session: Session, job_id: UUID) -> list[JobReviewOut]:
    """Both reviews for a job (0-2), explicitly constructed — no party ids,
    and `comment` reads through `public_comment` so a hidden comment is null
    even to the job's own parties."""
    out: list[JobReviewOut] = []
    review = session.scalar(select(Review).where(Review.job_id == job_id))
    if review is not None:
        out.append(
            JobReviewOut(
                id=review.id,
                kind=ReportTargetKind.REVIEW.value,
                rating=review.rating,
                comment=review.public_comment,
                created_at=review.created_at,
            )
        )
    seller_review = session.scalar(select(SellerReview).where(SellerReview.job_id == job_id))
    if seller_review is not None:
        out.append(
            JobReviewOut(
                id=seller_review.id,
                kind=ReportTargetKind.SELLER_REVIEW.value,
                rating=seller_review.rating,
                comment=seller_review.public_comment,
                created_at=seller_review.created_at,
            )
        )
    return out


def review_job(session: Session, buyer_id: str, job_id: UUID, body: ReviewRequest) -> Review:
    _require_active(session, buyer_id)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
    if job.status != JobStatus.COMPLETED or job.seller_id is None:
        raise ConflictError("can only review a completed job")
    if session.scalar(select(Review).where(Review.job_id == job_id)) is not None:
        raise ConflictError("job already reviewed")

    seller = repo.get_or_create_seller(session, job.seller_id)
    seller.rating_count += 1
    seller.rating_sum += body.rating
    # get_or_create_seller's session.get() autoflushes any pending INSERT, so
    # the review is added only after it — otherwise the UNIQUE(job_id)
    # violation would surface uncaught by that autoflush, not by the guarded
    # flush() below.
    review = Review(
        job_id=job.id,
        buyer_id=buyer_id,
        seller_id=job.seller_id,
        rating=body.rating,
        comment=body.comment,
    )
    session.add(review)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise ConflictError("job already reviewed") from None
    return review


def list_job_reviews_buyer(session: Session, buyer_id: str, job_id: UUID) -> list[JobReviewOut]:
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise NotFoundError("job not found")
    return _job_reviews(session, job_id)


def list_job_reviews_seller(session: Session, seller_id: str, job_id: UUID) -> list[JobReviewOut]:
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise NotFoundError("job not found")
    return _job_reviews(session, job_id)


def review_buyer(
    session: Session, seller_id: str, job_id: UUID, body: ReviewRequest
) -> SellerReview:
    _require_active(session, seller_id)
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise NotFoundError("job not found")
    if job.status != JobStatus.COMPLETED:
        raise ConflictError("can only review a completed job")
    if session.scalar(select(SellerReview).where(SellerReview.job_id == job_id)) is not None:
        raise ConflictError("job already reviewed")

    buyer = repo.get_or_create_buyer(session, job.buyer_id)
    buyer.rating_count += 1
    buyer.rating_sum += body.rating
    # get_or_create_buyer's session.get() autoflushes any pending INSERT, so
    # the review is added only after it — otherwise the UNIQUE(job_id)
    # violation would surface uncaught by that autoflush, not by the guarded
    # flush() below.
    review = SellerReview(
        job_id=job.id,
        seller_id=seller_id,
        buyer_id=job.buyer_id,
        rating=body.rating,
        comment=body.comment,
    )
    session.add(review)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise ConflictError("job already reviewed") from None
    return review


def _admin_review_out(row: Review | SellerReview) -> AdminReviewOut:
    author, subject = (
        (row.buyer_id, row.seller_id) if isinstance(row, Review) else (row.seller_id, row.buyer_id)
    )
    return AdminReviewOut(
        id=row.id,
        job_id=row.job_id,
        author_id=author,
        subject_id=subject,
        rating=row.rating,
        comment=row.comment,
        comment_hidden=row.comment_hidden,
        created_at=row.created_at,
    )


def admin_list_reviews(
    session: Session, kind: Literal["buyer", "seller"], limit: int, offset: int
) -> list[AdminReviewOut]:
    model = Review if kind == "buyer" else SellerReview
    stmt = select(model).order_by(model.created_at.desc(), model.id.desc())
    return [_admin_review_out(r) for r in _paginate(session, stmt, limit, offset)]


def _set_comment_hidden(
    kind: Literal["buyer", "seller"],
    review_id: UUID,
    hidden: bool,
    session: Session,
    admin_id: str,
) -> AdminReviewOut:
    model = Review if kind == "buyer" else SellerReview
    row = session.get(model, review_id)
    if row is None:
        raise NotFoundError("review not found")
    if row.comment_hidden == hidden:
        raise ConflictError("review already in that state")
    row.comment_hidden = hidden
    audit(
        session,
        admin_id,
        "hide_review" if hidden else "unhide_review",
        f"{kind}:{review_id}",
        {},
    )
    session.flush()
    return _admin_review_out(row)


def admin_hide_review(
    session: Session, admin_id: str, kind: Literal["buyer", "seller"], review_id: UUID
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, True, session, admin_id)


def admin_unhide_review(
    session: Session, admin_id: str, kind: Literal["buyer", "seller"], review_id: UUID
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, False, session, admin_id)
