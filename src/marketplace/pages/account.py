"""Account pages: signup, login, logout. Cookie sessions are the same
`auth_sessions` rows as API sessions; these handlers call the exact
`auth.signup`/`auth.login`/`auth.logout` the JSON API uses."""

import contextlib
from typing import Annotated

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from .. import auth
from ..db import CommitRoute
from ..models import LoginRequest, SignupRequest
from ..settings import settings
from .common import SESSION_COOKIE, MailDep, OptionalClaims, SessionDep, nav_context, render

router = APIRouter(route_class=CommitRoute, tags=["pages"])

_LOGIN_ERROR = "Invalid email or password."
_SIGNUP_ERROR = "We couldn't create that account. Check your details and try again."


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        # Lax keeps the cookie off cross-site subresource requests and
        # cross-site POSTs in browsers that enforce it; authenticated page
        # forms additionally carry a session-bound CSRF token (csrf.py).
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, session: SessionDep, claims: OptionalClaims) -> HTMLResponse:
    # A logged-in user hitting GET /signup still sees the form; only the nav
    # reflects the real session.
    return render(request, "signup.html", {**nav_context(session, claims), "error": None})


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, session: SessionDep, claims: OptionalClaims) -> HTMLResponse:
    return render(request, "login.html", {**nav_context(session, claims), "error": None})


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    session: SessionDep,
    mail: MailDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
) -> Response:
    # Reuse the exact signup logic the JSON API uses (hashing, the domain
    # record, the verification email, session issuance). A validation or
    # HTTPException failure re-renders the form instead of a JSON error.
    try:
        body = SignupRequest(email=email, password=password, role=role, display_name=display_name)  # pyright: ignore[reportArgumentType]
        result = auth.signup(body, session, mail)
    except (ValidationError, HTTPException):
        session.rollback()
        return render(
            request,
            "signup.html",
            {"claims": None, "current_user": None, "error": _SIGNUP_ERROR},
            status_code=400,
        )
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, result.token)
    return response


def _login_failure(request: Request) -> HTMLResponse:
    # Same generic copy for every failure mode (bad password, unknown
    # account, disallowed role): no signal about which one it was.
    return render(
        request,
        "login.html",
        {"claims": None, "current_user": None, "error": _LOGIN_ERROR},
        status_code=401,
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
) -> Response:
    # The login form only offers buyer/seller, so a raw POST with role=admin
    # (and the true admin password) must not reach an admin session through
    # this page surface. Rejected before auth.login with the same generic copy
    # as bad credentials, so nothing reveals that admin exists.
    if role not in ("buyer", "seller"):
        return _login_failure(request)
    try:
        body = LoginRequest(email=email, password=password, role=role)  # pyright: ignore[reportArgumentType]
        result = auth.login(body, session)
    except (ValidationError, HTTPException):
        return _login_failure(request)
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, result.token)
    return response


@router.post("/logout")
def logout_submit(
    session: SessionDep,
    session_token: Annotated[str | None, Cookie()] = None,
) -> Response:
    if session_token:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=session_token)
        # Already gone/expired: clearing the cookie is still correct.
        with contextlib.suppress(HTTPException):
            auth.logout(session, credentials)
    response = RedirectResponse(url="/", status_code=303)
    clear_session_cookie(response)
    return response
