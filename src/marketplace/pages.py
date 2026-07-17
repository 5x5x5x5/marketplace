"""Server-rendered pages: the browse storefront (phase 2 foundation) plus
cookie-backed signup/login/logout pages built on the existing auth_sessions
machinery.

Import direction: pages -> (db, auth, catalog, entities, models). NEVER
import api (api includes this router)."""

import contextlib
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import auth, catalog
from .db import CommitRoute, get_session
from .entities import Listing, SellerProfile, User
from .mail import EmailSender, get_mail_sender
from .models import CatalogSellerProfileOut, LoginRequest, SignupRequest
from .settings import settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
pages_router = APIRouter(route_class=CommitRoute, tags=["pages"])

_SessionDep = Annotated[Session, Depends(get_session)]
_OptionalClaims = Annotated[auth.Claims | None, Depends(auth.optional_claims)]
_MailDep = Annotated[EmailSender, Depends(get_mail_sender)]
_PAGE_SIZE = 24

_SESSION_COOKIE = "session_token"
_LOGIN_ERROR = "Invalid email or password."
_SIGNUP_ERROR = "We couldn't create that account. Check your details and try again."


def _nav_context(session: Session, claims: auth.Claims | None) -> dict[str, object]:
    """Shared context for base.html's nav: the raw claims (role/sub) plus the
    User row for its display_name, since Claims itself carries neither."""
    current_user = session.get(User, claims.sub) if claims is not None else None
    return {"claims": claims, "current_user": current_user}


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@pages_router.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    session: _SessionDep,
    claims: _OptionalClaims,
    service_type_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    stmt = catalog.visible_listings_stmt().order_by(Listing.created_at.desc())
    if service_type_id is not None:
        stmt = stmt.where(Listing.service_type_id == service_type_id)
    offset = (page - 1) * _PAGE_SIZE
    rows = session.execute(stmt.offset(offset).limit(_PAGE_SIZE + 1)).all()
    has_next = len(rows) > _PAGE_SIZE
    listings = catalog.catalog_listings_for_rows(session, rows[:_PAGE_SIZE])
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **_nav_context(session, claims),
            "listings": listings,
            "service_type_id": service_type_id,
            "page": page,
            "has_next": has_next,
        },
    )


def _not_found(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"claims": None, "current_user": None, "status_code": 404, "message": message},
        status_code=404,
    )


@pages_router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(
    request: Request, session: _SessionDep, claims: _OptionalClaims, listing_id: UUID
) -> HTMLResponse:
    row = session.execute(catalog.visible_listings_stmt().where(Listing.id == listing_id)).first()
    if row is None:
        return _not_found(request, "This listing isn't available.")
    listing, user, seller = row
    price = catalog.indicative_buyer_price(session, listing.service_type_id)
    if price is None:
        return _not_found(request, "This listing isn't available.")
    listing_out = catalog.catalog_listing_out(listing, user, seller, price)
    return templates.TemplateResponse(
        request,
        "listing.html",
        {**_nav_context(session, claims), "listing": listing_out},
    )


@pages_router.get("/sellers/{seller_id}", response_class=HTMLResponse)
def seller_profile(
    request: Request, session: _SessionDep, claims: _OptionalClaims, seller_id: str
) -> HTMLResponse:
    user = session.get(User, seller_id)
    seller = session.get(SellerProfile, seller_id)
    if not catalog.seller_publicly_visible(user, seller):
        return _not_found(request, "This seller isn't available.")
    assert user is not None
    assert seller is not None
    rows = session.execute(
        catalog.visible_listings_stmt().where(Listing.seller_id == seller_id)
    ).all()
    listings = catalog.catalog_listings_for_rows(session, rows)
    seller_summary = catalog.catalog_seller_out(user, seller)
    seller_out = CatalogSellerProfileOut(
        **seller_summary.model_dump(),
        completed_jobs=seller.completed_jobs,
        listings=listings,
    )
    return templates.TemplateResponse(
        request,
        "seller.html",
        {**_nav_context(session, claims), "seller": seller_out},
    )


@pages_router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "signup.html", {"claims": None, "current_user": None, "error": None}
    )


@pages_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"claims": None, "current_user": None, "error": None}
    )


@pages_router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    session: _SessionDep,
    mail: _MailDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
) -> Response:
    # Reuse the exact signup logic the JSON API uses (hashing, the domain
    # record, the verification email, session issuance) — never duplicated
    # here. A Pydantic/HTTPException failure re-renders the form instead of
    # bubbling a JSON error page.
    try:
        body = SignupRequest(email=email, password=password, role=role, display_name=display_name)  # pyright: ignore[reportArgumentType]
        result = auth.signup(body, session, mail)
    except (ValidationError, HTTPException):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"claims": None, "current_user": None, "error": _SIGNUP_ERROR},
            status_code=400,
        )
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, result.token)
    return response


@pages_router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    session: _SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
) -> Response:
    # Reuse the exact login logic the JSON API uses (uniform-401 lookup +
    # verify) so wrong-password and no-such-user stay indistinguishable here
    # too — same generic copy either way, no account-enumeration signal.
    try:
        body = LoginRequest(email=email, password=password, role=role)  # pyright: ignore[reportArgumentType]
        result = auth.login(body, session)
    except (ValidationError, HTTPException):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"claims": None, "current_user": None, "error": _LOGIN_ERROR},
            status_code=401,
        )
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, result.token)
    return response


@pages_router.post("/logout")
def logout_submit(
    session: _SessionDep,
    session_token: Annotated[str | None, Cookie()] = None,
) -> Response:
    if session_token:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=session_token)
        # Already gone/expired — clearing the cookie is still correct.
        with contextlib.suppress(HTTPException):
            auth.logout(session, credentials)
    response = RedirectResponse(url="/", status_code=303)
    _clear_session_cookie(response)
    return response
