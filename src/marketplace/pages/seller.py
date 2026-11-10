"""Seller pages: the dashboard (payments, availability, offers, jobs,
listings) and its forms.

Accept and complete are money paths; they are the unchanged
`jobs.accept_offer`/`jobs.complete_job` services. Contexts are seller views
(`SellerOfferView`, `SellerJobView`, `SellerListingOut`, `SellerProfileOut`):
never the buyer's price or identity."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ..db import CommitRoute
from ..errors import DomainError, NotFoundError
from ..models import (
    AvailabilityRequest,
    JobStatus,
    ListingCreate,
    ListingUpdate,
    OfferStatus,
    SellerJobView,
    SellerListingOut,
    SellerOfferView,
    SellerProfileOut,
)
from ..services import jobs, listings, payouts, profiles
from .common import (
    PAGE_SIZE,
    OptionalClaims,
    ProviderDep,
    SellerPage,
    SessionDep,
    error_page,
    nav_context,
    redirect,
    redirect_after_error,
    render,
)
from .csrf import CSRF

router = APIRouter(route_class=CommitRoute, tags=["pages"])

_DASHBOARD = "/seller"
_TRUE = "true"


def _photo_urls(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _form_errors(exc: ValidationError) -> list[str]:
    messages: list[str] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"] if not isinstance(part, int))
        messages.append(f"{field.replace('_', ' ')}: {err['msg']}")
    return messages


@router.get("/seller", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    seller_id: SellerPage,
    provider: ProviderDep,
) -> HTMLResponse:
    offers = jobs.list_offers(session, seller_id, provider, OfferStatus.OFFERED, PAGE_SIZE, 0)
    seller_jobs = jobs.list_seller_jobs(session, seller_id, None, PAGE_SIZE, 0)
    return render(
        request,
        "seller_dashboard.html",
        {
            **nav_context(session, claims),
            "profile": SellerProfileOut.model_validate(profiles.get_profile(session, seller_id)),
            "availability": profiles.availability_by_service_type(session, seller_id),
            "offers": [SellerOfferView.model_validate(o) for o in offers],
            "jobs": [SellerJobView.model_validate(j) for j in seller_jobs],
            "listings": [
                SellerListingOut.model_validate(x) for x in listings.my_listings(session, seller_id)
            ],
            "accepted": JobStatus.ACCEPTED,
        },
    )


@router.get("/onboarded")
def onboarding_return() -> RedirectResponse:
    """Where the provider's hosted onboarding sends the seller back
    (`ONBOARDING_RETURN_URL` defaults to BASE_URL/onboarded)."""
    return redirect(_DASHBOARD, "onboarding_returned")


@router.post("/seller/payments/onboard", dependencies=[CSRF])
def onboard(session: SessionDep, seller_id: SellerPage, provider: ProviderDep) -> Response:
    try:
        result = payouts.onboard_payments(session, seller_id, provider)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc)
    if result.payments_ready:
        return redirect(_DASHBOARD, "payments_ready")
    # The provider's hosted onboarding; it returns the seller to /onboarded.
    return RedirectResponse(result.onboarding_url, status_code=303)


@router.post("/seller/availability", dependencies=[CSRF])
def set_availability(
    session: SessionDep,
    seller_id: SellerPage,
    service_type_id: Annotated[str, Form()],
    on: Annotated[str, Form()],
) -> Response:
    # The form sends the target state, not "flip", so a repeat is a no-op.
    try:
        if on == _TRUE:
            req = AvailabilityRequest(service_type_id=service_type_id)
            profiles.post_availability(session, seller_id, req)
            return redirect(_DASHBOARD, "available_on")
        try:
            profiles.delete_availability(session, seller_id, service_type_id)
        except NotFoundError:
            session.rollback()  # already off: the target state holds
        return redirect(_DASHBOARD, "available_off")
    except ValidationError:
        return redirect(_DASHBOARD, "invalid")
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc)


@router.post("/seller/offers/{offer_id}/accept", dependencies=[CSRF])
def accept(
    session: SessionDep, seller_id: SellerPage, provider: ProviderDep, offer_id: UUID
) -> Response:
    try:
        job = jobs.accept_offer(session, seller_id, offer_id, provider)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc, {409: "accept_conflict"})
    pending = job.status is JobStatus.AWAITING_PAYMENT
    return redirect(_DASHBOARD, "accepted_pending" if pending else "accepted")


@router.post("/seller/offers/{offer_id}/decline", dependencies=[CSRF])
def decline(session: SessionDep, seller_id: SellerPage, offer_id: UUID) -> Response:
    try:
        jobs.decline_offer(session, seller_id, offer_id)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc, {409: "accept_conflict"})
    return redirect(_DASHBOARD, "declined")


@router.post("/seller/jobs/{job_id}/complete", dependencies=[CSRF])
def complete(
    session: SessionDep, seller_id: SellerPage, provider: ProviderDep, job_id: UUID
) -> Response:
    try:
        jobs.complete_job(session, seller_id, job_id, provider)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc, {409: "complete_conflict"})
    return redirect(_DASHBOARD, "completed")


def _listing_form(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    seller_id: str,
    values: dict[str, str],
    *,
    listing_id: UUID | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "listing_form.html",
        {
            **nav_context(session, claims),
            "listing_id": listing_id,
            "values": values,
            "errors": errors or [],
            "service_types": [
                sid for sid, _ in profiles.availability_by_service_type(session, seller_id)
            ],
        },
        status_code=status_code,
    )


@router.get("/seller/listings/new", response_class=HTMLResponse)
def new_listing(
    request: Request, session: SessionDep, claims: OptionalClaims, seller_id: SellerPage
) -> HTMLResponse:
    return _listing_form(request, session, claims, seller_id, {})


@router.post("/seller/listings", dependencies=[CSRF])
def create_listing(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    seller_id: SellerPage,
    service_type_id: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    photo_urls: Annotated[str, Form()] = "",
) -> Response:
    values = {
        "service_type_id": service_type_id,
        "title": title,
        "description": description,
        "photo_urls": photo_urls,
    }
    try:
        body = ListingCreate(
            service_type_id=service_type_id,
            title=title,
            description=description,
            photo_urls=_photo_urls(photo_urls),
        )
    except ValidationError as exc:
        return _listing_form(
            request, session, claims, seller_id, values, errors=_form_errors(exc), status_code=400
        )
    try:
        listings.create_listing(session, seller_id, body)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc, {409: "onboard_first"})
    return redirect(_DASHBOARD, "listing_created")


@router.get("/seller/listings/{listing_id}/edit", response_class=HTMLResponse)
def edit_listing(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    seller_id: SellerPage,
    listing_id: UUID,
) -> HTMLResponse:
    try:
        listing = SellerListingOut.model_validate(
            listings.get_own_listing(session, seller_id, listing_id)
        )
    except NotFoundError:
        return error_page(request, session, claims, 404, "Listing not found.")
    values = {
        "service_type_id": listing.service_type_id,
        "title": listing.title,
        "description": listing.description,
        "photo_urls": "\n".join(listing.photo_urls),
    }
    return _listing_form(request, session, claims, seller_id, values, listing_id=listing_id)


@router.post("/seller/listings/{listing_id}", dependencies=[CSRF])
def update_listing(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    seller_id: SellerPage,
    listing_id: UUID,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    photo_urls: Annotated[str, Form()] = "",
) -> Response:
    values = {"title": title, "description": description, "photo_urls": photo_urls}
    try:
        body = ListingUpdate(
            title=title, description=description, photo_urls=_photo_urls(photo_urls)
        )
    except ValidationError as exc:
        return _listing_form(
            request,
            session,
            claims,
            seller_id,
            values,
            listing_id=listing_id,
            errors=_form_errors(exc),
            status_code=400,
        )
    try:
        listings.update_listing(session, seller_id, listing_id, body)
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc)
    return redirect(_DASHBOARD, "listing_saved")


@router.post("/seller/listings/{listing_id}/active", dependencies=[CSRF])
def set_listing_active(
    session: SessionDep,
    seller_id: SellerPage,
    listing_id: UUID,
    active: Annotated[str, Form()],
) -> Response:
    # The seller-owned flag only (`hidden` is admin takedown); the form sends
    # the target value, so a repeat is a no-op.
    target = active == _TRUE
    try:
        listings.update_listing(session, seller_id, listing_id, ListingUpdate(active=target))
    except DomainError as exc:
        return redirect_after_error(session, _DASHBOARD, exc)
    return redirect(_DASHBOARD, "listing_on" if target else "listing_off")
