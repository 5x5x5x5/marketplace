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
from ..models import (
    LoginRequest,
    ResetConfirmRequest,
    ResetRequest,
    SignupRequest,
    UserRole,
    VerifyRequest,
)
from ..settings import settings
from .common import (
    SESSION_COOKIE,
    MailDep,
    OptionalClaims,
    ParticipantPage,
    SessionDep,
    nav_context,
    redirect,
    render,
)
from .csrf import CSRF

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


@router.post("/logout", dependencies=[CSRF])
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


# ---------- verify email and password reset ----------
#
# The mailed links carry a raw single-use token in the query string. A GET
# never consumes it (mail scanners prefetch links): it renders a form that
# POSTs the token. The access log drops query strings and never logs bodies,
# and these pages answer `Cache-Control: no-store` (render adds
# `Referrer-Policy: same-origin`), so the token stays out of logs, caches and
# Referer headers.

_NO_STORE = {"Cache-Control": "no-store"}
_BAD_LINK = "This link is invalid or has expired. Request a new one."


def _message(
    request: Request,
    session: SessionDep,
    claims: auth.Claims | None,
    title: str,
    body: str,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "message.html",
        {**nav_context(session, claims), "title": title, "body": body},
        status_code=status_code,
        headers=_NO_STORE,
    )


@router.get("/verify", response_class=HTMLResponse)
def verify_form(
    request: Request, session: SessionDep, claims: OptionalClaims, token: str = ""
) -> HTMLResponse:
    return render(
        request,
        "verify.html",
        {**nav_context(session, claims), "token": token},
        headers=_NO_STORE,
    )


@router.post("/verify", response_class=HTMLResponse)
def verify_submit(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    token: Annotated[str, Form()] = "",
) -> Response:
    try:
        auth.verify_email(VerifyRequest(token=token), session)
    except (ValidationError, HTTPException):
        session.rollback()
        return _message(request, session, claims, "Verification failed", _BAD_LINK, 400)
    return RedirectResponse("/verify/done", status_code=303)


@router.get("/verify/done", response_class=HTMLResponse)
def verify_done(request: Request, session: SessionDep, claims: OptionalClaims) -> HTMLResponse:
    return _message(
        request, session, claims, "Email verified", "Thanks, your email address is verified."
    )


@router.post("/verify/resend", dependencies=[CSRF])
def verify_resend(session: SessionDep, mail: MailDep, claims: ParticipantPage) -> Response:
    sent = auth.resend_verification(session, mail, claims.sub)
    home = "/bookings" if claims.role is UserRole.BUYER else "/seller"
    return redirect(home, "verification_sent" if sent else "already_verified")


@router.get("/password-reset", response_class=HTMLResponse)
def reset_request_form(
    request: Request, session: SessionDep, claims: OptionalClaims
) -> HTMLResponse:
    return render(request, "reset_request.html", nav_context(session, claims))


@router.post("/password-reset/request")
def reset_request_submit(
    session: SessionDep,
    mail: MailDep,
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
) -> Response:
    # Same redirect whatever the input, malformed included: the page never
    # says whether an address has an account.
    try:
        auth.request_password_reset(ResetRequest(email=email, role=role), session, mail)  # pyright: ignore[reportArgumentType]
    except ValidationError:
        session.rollback()
    return RedirectResponse("/password-reset/sent", status_code=303)


@router.get("/password-reset/sent", response_class=HTMLResponse)
def reset_sent(request: Request, session: SessionDep, claims: OptionalClaims) -> HTMLResponse:
    return _message(
        request,
        session,
        claims,
        "Check your email",
        "If an account exists for that address and role, we've sent a link to reset its "
        "password. The link works once and expires in an hour.",
    )


def _reset_form(
    request: Request,
    session: SessionDep,
    claims: auth.Claims | None,
    token: str,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "reset_confirm.html",
        {**nav_context(session, claims), "token": token, "error": error},
        status_code=status_code,
        headers=_NO_STORE,
    )


@router.get("/password-reset/confirm", response_class=HTMLResponse)
def reset_confirm_form(
    request: Request, session: SessionDep, claims: OptionalClaims, token: str = ""
) -> HTMLResponse:
    return _reset_form(request, session, claims, token)


@router.post("/password-reset/confirm", response_class=HTMLResponse)
def reset_confirm_submit(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    token: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
) -> Response:
    try:
        body = ResetConfirmRequest(token=token, new_password=new_password)
    except ValidationError:
        error = "Choose a password of at least 8 characters."
        return _reset_form(request, session, claims, token, error, status_code=400)
    try:
        auth.confirm_password_reset(body, session)
    except HTTPException:
        session.rollback()
        return _message(request, session, claims, "Reset failed", _BAD_LINK, 400)
    # Every session of the account was just revoked, this browser's included.
    response = redirect("/login", "password_reset")
    clear_session_cookie(response)
    return response
