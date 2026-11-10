"""Buyer routes (`/v1/...`): quotes, jobs, cancel, profile, reviews and
disputes. Identity is the buyer token's subject; every response is a buyer
view. Each handler parses, calls one service, and returns its result."""

from uuid import UUID

from fastapi import APIRouter

from ..db import CommitRoute
from ..entities import BuyerProfile, Dispute, Job, Quote, Review
from ..models import (
    BuyerDisputeOut,
    BuyerJobView,
    BuyerProfileOut,
    DisputeRequest,
    JobCreateRequest,
    JobReviewOut,
    JobStatus,
    QuoteOut,
    QuoteRequest,
    ReviewOut,
    ReviewRequest,
)
from ..services import booking, disputes, jobs, profiles, reviews
from .deps import BuyerId, Limit, Offset, ProviderDep, SessionDep

buyer_router = APIRouter(prefix="/v1", tags=["buyer"], route_class=CommitRoute)


@buyer_router.post("/quotes", response_model=QuoteOut, status_code=201)
def create_quote(req: QuoteRequest, session: SessionDep, buyer_id: BuyerId) -> Quote:
    return booking.create_quote(session, buyer_id, req)


@buyer_router.post("/jobs", response_model=BuyerJobView, status_code=201)
def create_job(req: JobCreateRequest, session: SessionDep, buyer_id: BuyerId) -> Job:
    return booking.create_job(session, buyer_id, req)


@buyer_router.get("/jobs", response_model=list[BuyerJobView])
def list_buyer_jobs(
    session: SessionDep,
    buyer_id: BuyerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    return jobs.list_buyer_jobs(session, buyer_id, status, limit, offset)


@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep
) -> BuyerJobView:
    return jobs.get_job_buyer(session, buyer_id, job_id, provider)


@buyer_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def cancel_job(job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep) -> Job:
    return jobs.cancel_job(session, buyer_id, job_id, provider)


@buyer_router.get("/profile", response_model=BuyerProfileOut)
def get_buyer_profile(session: SessionDep, buyer_id: BuyerId) -> BuyerProfile:
    return profiles.get_buyer_profile(session, buyer_id)


@buyer_router.post("/jobs/{job_id}/review", response_model=ReviewOut, status_code=201)
def review_job(job_id: UUID, body: ReviewRequest, session: SessionDep, buyer_id: BuyerId) -> Review:
    return reviews.review_job(session, buyer_id, job_id, body)


@buyer_router.post("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut, status_code=201)
def open_dispute(
    job_id: UUID, body: DisputeRequest, session: SessionDep, buyer_id: BuyerId
) -> Dispute:
    return disputes.open_dispute(session, buyer_id, job_id, body)


@buyer_router.get("/jobs/{job_id}/dispute", response_model=BuyerDisputeOut)
def get_dispute_buyer(job_id: UUID, session: SessionDep, buyer_id: BuyerId) -> Dispute:
    return disputes.get_dispute_buyer(session, buyer_id, job_id)


@buyer_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_buyer(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId
) -> list[JobReviewOut]:
    return reviews.list_job_reviews_buyer(session, buyer_id, job_id)
