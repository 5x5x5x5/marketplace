"""Buyer pages: price a listing, confirm the booking, follow it, cancel it.

Every write is one service call (`booking.create_quote`, `booking.create_job`,
`jobs.cancel_job`); a `DomainError` is answered by `redirect_after_error`,
which rolls back first. Contexts are buyer views (`QuoteOut`,
`BuyerJobView`) plus public catalog views; never the seller's payout."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from .. import catalog
from ..db import CommitRoute
from ..errors import DomainError, GoneError, NotFoundError
from ..models import (
    BuyerJobView,
    CatalogListingOut,
    CatalogSellerOut,
    JobCreateRequest,
    JobStatus,
    PaymentStatus,
    QuoteOut,
    QuoteRequest,
)
from ..services import booking, jobs
from ..settings import settings
from .common import (
    PAGE_SIZE,
    BuyerPage,
    OptionalClaims,
    ProviderDep,
    SessionDep,
    error_page,
    nav_context,
    redirect,
    redirect_after_error,
    render,
)
from .csrf import CSRF

router = APIRouter(route_class=CommitRoute, tags=["pages"])

# Statuses a buyer can still cancel from, and the ones worth polling: the
# same set `jobs.cancel_job` accepts (the service stays the authority; this
# only decides whether to draw the button).
_OPEN = (JobStatus.PENDING, JobStatus.AWAITING_PAYMENT)


def _listing_or_none(session: SessionDep, listing_id: UUID | None) -> CatalogListingOut | None:
    if listing_id is None:
        return None
    try:
        return catalog.get_listing(session, listing_id)
    except NotFoundError:
        return None


@router.post("/listings/{listing_id}/quote", dependencies=[CSRF])
def request_quote(session: SessionDep, buyer_id: BuyerPage, listing_id: UUID) -> Response:
    try:
        quote = booking.create_quote(session, buyer_id, QuoteRequest(listing_id=listing_id))
    except DomainError as exc:
        return redirect_after_error(
            session,
            f"/listings/{listing_id}",
            exc,
            {409: "quote_unavailable", 422: "quote_unavailable"},
        )
    return redirect(f"/quotes/{quote.id}")


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_page(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    buyer_id: BuyerPage,
    quote_id: UUID,
) -> Response:
    try:
        quote = booking.get_quote(session, buyer_id, quote_id)
    except NotFoundError:
        # Quotes are deleted when booked: a reload or back-button lands on
        # the booking that consumed this quote, if there is one.
        job = booking.job_for_quote(session, buyer_id, quote_id)
        if job is not None:
            return redirect(f"/bookings/{job.id}")
        return error_page(request, session, claims, 404, "This price is no longer available.")
    view = QuoteOut.model_validate(quote)
    expired = view.expires_at < datetime.now(UTC)
    return render(
        request,
        "quote.html",
        {
            **nav_context(session, claims),
            "quote": view,
            "expired": expired,
            "listing": _listing_or_none(session, view.listing_id),
            "seller": catalog.public_seller(session, view.pinned_seller_id)
            if view.pinned_seller_id
            else None,
        },
        status_code=410 if expired else 200,
    )


@router.post("/quotes/{quote_id}/book", dependencies=[CSRF])
def book(session: SessionDep, buyer_id: BuyerPage, quote_id: UUID) -> Response:
    try:
        job = booking.create_job(session, buyer_id, JobCreateRequest(quote_id=quote_id))
    except NotFoundError:
        session.rollback()
        # A repeated submit: the first one consumed the quote and made a job.
        existing = booking.job_for_quote(session, buyer_id, quote_id)
        if existing is not None:
            return redirect(f"/bookings/{existing.id}")
        return redirect("/", "quote_unavailable")
    except GoneError as exc:
        # Two 410s: an expired quote (nothing changed) or a pinned listing
        # that went away (the service already committed the quote's
        # consumption). Tell them apart by whether the quote still exists.
        session.rollback()
        try:
            booking.get_quote(session, buyer_id, quote_id)
        except NotFoundError:
            return redirect("/", "quote_unavailable")
        return redirect_after_error(session, f"/quotes/{quote_id}", exc, {410: "quote_expired"})
    except DomainError as exc:
        return redirect_after_error(session, f"/quotes/{quote_id}", exc)
    return redirect(f"/bookings/{job.id}")


@router.get("/bookings", response_class=HTMLResponse)
def bookings(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    buyer_id: BuyerPage,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    rows = jobs.list_buyer_jobs(session, buyer_id, None, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    views = [BuyerJobView.model_validate(job) for job in rows[:PAGE_SIZE]]
    return render(
        request,
        "bookings.html",
        {
            **nav_context(session, claims),
            "jobs": views,
            "page": page,
            "has_next": len(rows) > PAGE_SIZE,
        },
    )


def _seller_for(session: SessionDep, job: BuyerJobView) -> CatalogSellerOut | None:
    seller_id = job.seller_id or job.pinned_seller_id
    return catalog.public_seller(session, seller_id) if seller_id else None


def _payment_context(job: BuyerJobView, provider: ProviderDep) -> dict[str, object]:
    """What the payment step needs, and only while a payment is pending.
    `client_secret` is set by `jobs.get_job_buyer` only while the job awaits
    payment, and only for the job's own buyer (the read is owner-checked).
    `browser_key` is None unless the provider runs a browser-side step."""
    pending = (
        job.status is JobStatus.AWAITING_PAYMENT
        and job.payment_status is PaymentStatus.PENDING
        and job.client_secret is not None
    )
    return {
        "payment_pending": pending,
        "browser_key": None,
        "client_secret": job.client_secret if pending else None,
        "return_url": f"{settings.base_url}/bookings/{job.id}",
    }


@router.get("/bookings/{job_id}", response_class=HTMLResponse)
def booking_page(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    buyer_id: BuyerPage,
    provider: ProviderDep,
    job_id: UUID,
) -> HTMLResponse:
    try:
        job = jobs.get_job_buyer(session, buyer_id, job_id, provider)
    except NotFoundError:
        return error_page(request, session, claims, 404, "Booking not found.")
    return render(
        request,
        "booking.html",
        {
            **nav_context(session, claims),
            "job": job,
            "seller": _seller_for(session, job),
            "can_cancel": job.status in _OPEN,
            "live": job.status in _OPEN,
            **_payment_context(job, provider),
        },
    )


@router.get("/bookings/{job_id}/status", response_class=HTMLResponse)
def booking_status(
    request: Request,
    session: SessionDep,
    buyer_id: BuyerPage,
    provider: ProviderDep,
    job_id: UUID,
    shown: JobStatus | None = None,
) -> HTMLResponse:
    """htmx poll target. When the status moved since the page was drawn, ask
    htmx to reload the whole page so the payment form and cancel button are
    re-drawn from server state."""
    try:
        job = jobs.get_job_buyer(session, buyer_id, job_id, provider)
    except NotFoundError:
        return render(request, "_booking_status.html", {"job": None}, status_code=404)
    headers = {"HX-Refresh": "true"} if shown is not None and job.status != shown else None
    return render(
        request,
        "_booking_status.html",
        {"job": job, "seller": _seller_for(session, job), "live": job.status in _OPEN},
        headers=headers,
    )


@router.post("/bookings/{job_id}/cancel", dependencies=[CSRF])
def cancel(
    session: SessionDep, buyer_id: BuyerPage, provider: ProviderDep, job_id: UUID
) -> Response:
    try:
        jobs.cancel_job(session, buyer_id, job_id, provider)
    except DomainError as exc:
        return redirect_after_error(session, f"/bookings/{job_id}", exc, {409: "cancel_conflict"})
    return redirect(f"/bookings/{job_id}", "cancelled")
