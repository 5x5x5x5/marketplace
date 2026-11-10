"""Shared page plumbing: rendering, the viewer's identity, notices, and the
one way a page answers a domain error.

Import direction: pages -> (services, auth, catalog, db, entities, models,
payments). Never `api` or `routers` (api mounts this package)."""

from collections.abc import Mapping
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_session
from ..entities import User
from ..errors import DomainError, EmailNotVerifiedError
from ..mail import EmailSender, get_mail_sender
from ..models import UserRole
from ..payments import get_provider
from ..payments.port import PaymentProvider
from .interrupts import LoginRequired, PageForbidden, templates

SessionDep = Annotated[Session, Depends(get_session)]
OptionalClaims = Annotated[auth.Claims | None, Depends(auth.optional_claims)]
MailDep = Annotated[EmailSender, Depends(get_mail_sender)]
ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]

SESSION_COOKIE = "session_token"
PAGE_SIZE = 24

# Notices are looked up by code, never reflected from the URL: a crafted
# link can choose which of these fixed messages shows, nothing more.
NOTICES: dict[str, str] = {
    "forbidden": "Your account can't do that right now.",
    "not_found": "That item no longer exists.",
    "conflict": "That can't be done in its current state. The page shows where things stand.",
    "gone": "That has expired.",
    "invalid": "That request couldn't be processed. Check the form and try again.",
    "provider": "The payment provider didn't respond. Please try again.",
    "busy": "We're busy right now. Please try again shortly.",
    "unverified": "Verify your email address first. Check your inbox, or send a new link.",
    "quote_unavailable": "This listing can't be booked right now.",
    "quote_expired": "That price has expired. Get a new one from the listing.",
    "cancelled": "Booking cancelled.",
    "cancel_conflict": "This booking can no longer be cancelled.",
    "accepted": "Offer accepted. The buyer has been charged.",
    "accepted_pending": "Offer accepted. Waiting for the buyer's payment to go through.",
    "accept_conflict": "That offer is no longer open, or you're at capacity.",
    "declined": "Offer declined.",
    "completed": "Job marked complete. Your payout is on its way.",
    "complete_conflict": "That job can't be completed in its current state.",
    "listing_created": "Listing created.",
    "listing_saved": "Listing saved.",
    "listing_on": "Listing is live in the catalog.",
    "listing_off": "Listing is hidden from the catalog.",
    "onboard_first": "Set up payments before creating a listing.",
    "available_on": "You're available for this service.",
    "available_off": "You're no longer available for this service.",
    "payments_ready": "Payments are set up.",
    "onboarding_returned": "Welcome back. Your payments status is shown below.",
    "verification_sent": "We've sent a new verification link to your email address.",
    "already_verified": "Your email address is already verified.",
    "password_reset": "Your password was changed. Log in with the new one.",
}

_DEFAULT_BY_STATUS = {
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    410: "gone",
    422: "invalid",
    502: "provider",
    503: "busy",
}


def notice_message(code: str | None) -> str | None:
    return NOTICES.get(code) if code else None


def nav_context(session: Session, claims: auth.Claims | None) -> dict[str, object]:
    """Context for base.html's nav: the claims (role/sub) plus the viewer's own
    User row for its display name, since Claims carries neither."""
    current_user = session.get(User, claims.sub) if claims is not None else None
    return {"claims": claims, "current_user": current_user}


def render(
    request: Request,
    name: str,
    context: Mapping[str, Any],
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """Render a page. Every page answers with `Referrer-Policy: same-origin`
    so a token in the address bar (a reset link, Stripe's return parameters)
    never leaves in a Referer, and a logged-in page is never cached."""
    ctx: dict[str, Any] = {"notice": notice_message(request.query_params.get("notice"))}
    ctx.update(context)
    out_headers = {"Referrer-Policy": "same-origin"}
    if request.cookies.get(SESSION_COOKIE):
        out_headers["Cache-Control"] = "no-store"
    out_headers.update(headers or {})
    return templates.TemplateResponse(
        request, name, ctx, status_code=status_code, headers=out_headers
    )


def error_page(
    request: Request,
    session: Session,
    claims: auth.Claims | None,
    status_code: int,
    message: str,
) -> HTMLResponse:
    return render(
        request,
        "error.html",
        {**nav_context(session, claims), "status_code": status_code, "message": message},
        status_code=status_code,
    )


def redirect(url: str, notice: str | None = None) -> RedirectResponse:
    if notice:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode({'notice': notice})}"
    return RedirectResponse(url, status_code=303)


def redirect_after_error(
    session: Session,
    url: str,
    exc: DomainError,
    notices: Mapping[int, str] | None = None,
) -> RedirectResponse:
    """The one way a page POST answers a service's DomainError.

    Rolls back first: a 303 is under 400, so `CommitRoute` would otherwise
    commit whatever the service mutated before it raised (the JSON API
    discards that by answering 4xx). Work a service committed itself before
    raising (the pinned-booking 410) is already durable and stays."""
    session.rollback()
    if isinstance(exc, EmailNotVerifiedError):
        code = "unverified"
    else:
        code = (notices or {}).get(exc.status_code) or _DEFAULT_BY_STATUS.get(
            exc.status_code, "invalid"
        )
    return redirect(url, code)


def page_buyer(claims: OptionalClaims) -> str:
    if claims is None:
        raise LoginRequired
    if claims.role is not UserRole.BUYER:
        raise PageForbidden("This page is for buyer accounts. Log in as a buyer to book.")
    return claims.sub


def page_seller(claims: OptionalClaims) -> str:
    if claims is None:
        raise LoginRequired
    if claims.role is not UserRole.SELLER:
        raise PageForbidden("This page is for seller accounts.")
    return claims.sub


def page_participant(claims: OptionalClaims) -> auth.Claims:
    if claims is None:
        raise LoginRequired
    if claims.role is UserRole.ADMIN:
        raise PageForbidden("Admin accounts use the API, not these pages.")
    return claims


BuyerPage = Annotated[str, Depends(page_buyer)]
SellerPage = Annotated[str, Depends(page_seller)]
ParticipantPage = Annotated[auth.Claims, Depends(page_participant)]
