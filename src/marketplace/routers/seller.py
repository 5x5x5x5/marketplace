"""Seller routes (`/v1/seller/...`): profile, onboarding, availability,
offers, jobs, listings, reviews and disputes. Identity is the seller token's
subject; every response is a seller view."""

from uuid import UUID

from fastapi import APIRouter

from ..db import CommitRoute
from ..entities import Dispute, Job, Listing, Offer, SellerProfile, SellerReview, Transaction
from ..models import (
    AvailabilityRequest,
    JobReviewOut,
    JobStatus,
    ListingCreate,
    ListingUpdate,
    OfferStatus,
    OnboardingOut,
    ReviewRequest,
    SellerCompletionOut,
    SellerDisputeOut,
    SellerJobView,
    SellerListingOut,
    SellerOfferView,
    SellerProfileOut,
    SellerProfileUpdate,
    SellerReviewOut,
)
from ..services import disputes, jobs, listings, payouts, profiles, reviews
from .deps import Limit, Offset, ProviderDep, SellerId, SessionDep

seller_router = APIRouter(prefix="/v1/seller", tags=["seller"], route_class=CommitRoute)


@seller_router.put("/profile", response_model=SellerProfileOut)
def update_profile(
    body: SellerProfileUpdate, session: SessionDep, seller_id: SellerId
) -> SellerProfile:
    return profiles.update_profile(session, seller_id, body)


@seller_router.get("/profile", response_model=SellerProfileOut)
def get_profile(session: SessionDep, seller_id: SellerId) -> SellerProfile:
    return profiles.get_profile(session, seller_id)


@seller_router.post("/payments/onboard", response_model=OnboardingOut)
def onboard_payments(
    session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> OnboardingOut:
    """Create the seller's payment account (once) and return the onboarding link.

    `payments_ready` flips via the provider's account webhook (instantly for the
    fake provider); matching only offers jobs to ready sellers."""
    return payouts.onboard_payments(session, seller_id, provider)


@seller_router.post("/availability")
def post_availability(
    req: AvailabilityRequest, session: SessionDep, seller_id: SellerId
) -> dict[str, str]:
    profiles.post_availability(session, seller_id, req)
    return {"status": "ok"}


@seller_router.delete("/availability/{service_type_id}")
def delete_availability(
    service_type_id: str, session: SessionDep, seller_id: SellerId
) -> dict[str, str]:
    profiles.delete_availability(session, seller_id, service_type_id)
    return {"status": "ok"}


@seller_router.get("/offers", response_model=list[SellerOfferView])
def list_offers(
    session: SessionDep,
    seller_id: SellerId,
    provider: ProviderDep,
    status: OfferStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Offer]:
    return jobs.list_offers(session, seller_id, provider, status, limit, offset)


@seller_router.get("/jobs", response_model=list[SellerJobView])
def list_seller_jobs(
    session: SessionDep,
    seller_id: SellerId,
    status: JobStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[Job]:
    return jobs.list_seller_jobs(session, seller_id, status, limit, offset)


@seller_router.get("/jobs/{job_id}/dispute", response_model=SellerDisputeOut)
def get_dispute_seller(job_id: UUID, session: SessionDep, seller_id: SellerId) -> Dispute:
    return disputes.get_dispute_seller(session, seller_id, job_id)


@seller_router.get("/jobs/{job_id}/reviews", response_model=list[JobReviewOut])
def list_job_reviews_seller(
    job_id: UUID, session: SessionDep, seller_id: SellerId
) -> list[JobReviewOut]:
    return reviews.list_job_reviews_seller(session, seller_id, job_id)


@seller_router.post("/jobs/{job_id}/review", response_model=SellerReviewOut, status_code=201)
def review_buyer(
    job_id: UUID, body: ReviewRequest, session: SessionDep, seller_id: SellerId
) -> SellerReview:
    return reviews.review_buyer(session, seller_id, job_id, body)


@seller_router.post("/listings", response_model=SellerListingOut, status_code=201)
def create_listing(body: ListingCreate, session: SessionDep, seller_id: SellerId) -> Listing:
    return listings.create_listing(session, seller_id, body)


@seller_router.get("/listings", response_model=list[SellerListingOut])
def my_listings(session: SessionDep, seller_id: SellerId) -> list[Listing]:
    return listings.my_listings(session, seller_id)


@seller_router.put("/listings/{listing_id}", response_model=SellerListingOut)
def update_listing(
    listing_id: UUID, body: ListingUpdate, session: SessionDep, seller_id: SellerId
) -> Listing:
    return listings.update_listing(session, seller_id, listing_id, body)


@seller_router.post("/offers/{offer_id}/accept", response_model=SellerJobView)
def accept_offer(
    offer_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Job:
    return jobs.accept_offer(session, seller_id, offer_id, provider)


@seller_router.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: UUID, session: SessionDep, seller_id: SellerId) -> dict[str, str]:
    jobs.decline_offer(session, seller_id, offer_id)
    return {"status": "ok"}


@seller_router.post("/jobs/{job_id}/complete", response_model=SellerCompletionOut)
def complete_job(
    job_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Transaction:
    return jobs.complete_job(session, seller_id, job_id, provider)
