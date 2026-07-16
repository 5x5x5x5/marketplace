"""Session-backed authentication.

Login (the auth endpoints, added alongside) stores a session row keyed by the
sha256 of an opaque bearer token; the dependencies below resolve that token to
``(role, sub=user_id)`` with one indexed lookup. Endpoints derive
``buyer_id``/``seller_id`` from the authenticated principal — never a request
body — so identity cannot be spoofed. Sessions are revocable: logout, bans,
and password resets delete rows.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import repo
from .db import SessionLocal, get_session
from .entities import AuthSession, EmailToken, User
from .mail import EmailSender, get_mail_sender
from .models import (
    EmailTokenPurpose,
    LoginRequest,
    ResetConfirmRequest,
    ResetRequest,
    SessionOut,
    SignupRequest,
    UserOut,
    UserRole,
    VerifyRequest,
)
from .settings import settings

_password_hash = PasswordHash.recommended()  # argon2id


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


# Verified against when login hits an unknown email, so response timing does
# not reveal whether an account exists.
_DUMMY_HASH = _password_hash.hash("timing-equalizer")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def create_session(db: Session, user: User) -> tuple[str, datetime]:
    """Issue an opaque bearer for ``user``; only its sha256 is stored."""
    raw = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(hours=settings.session_ttl_hours)
    db.add(AuthSession(user_id=user.id, token_hash=_hash_token(raw), expires_at=expires_at))
    db.flush()
    return raw, expires_at


@dataclass(frozen=True)
class Claims:
    role: UserRole
    sub: str  # user id


def _resolve_bearer(db: Session, authorization: str) -> Claims | None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    user = db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(AuthSession.token_hash == _hash_token(token), AuthSession.expires_at > _now())
    )
    if user is None:
        return None
    return Claims(role=user.role, sub=user.id)


_SessionDep = Annotated[Session, Depends(get_session)]


_bearer_scheme = HTTPBearer(
    auto_error=False,  # our own 401 shape, and /docs marks endpoints instead of erroring
    description="Session token from POST /v1/auth/signup or /v1/auth/login",
)


def _principal(
    session: _SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> Claims:
    authorization = f"{credentials.scheme} {credentials.credentials}" if credentials else ""
    claims = _resolve_bearer(session, authorization)
    if claims is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return claims


Principal = Annotated[Claims, Depends(_principal)]


def require_admin(claims: Principal) -> str:
    if claims.role is not UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin credentials required")
    return claims.sub


def current_buyer(claims: Principal) -> str:
    if claims.role is not UserRole.BUYER:
        raise HTTPException(status_code=403, detail="buyer credentials required")
    return claims.sub


def current_seller(claims: Principal) -> str:
    if claims.role is not UserRole.SELLER:
        raise HTTPException(status_code=403, detail="seller credentials required")
    return claims.sub


def current_participant(claims: Principal) -> Claims:
    """Buyer or seller — the roles that can file reports."""
    if claims.role is UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="buyer or seller credentials required")
    return claims


def peek_principal(db: Session, authorization: str | None) -> str | None:
    """Best-effort principal ("role:sub") for middleware. None when absent or
    invalid — the strict endpoint dependencies still produce the real 401."""
    if not authorization:
        return None
    claims = _resolve_bearer(db, authorization)
    return None if claims is None else f"{claims.role}:{claims.sub}"


logger = logging.getLogger("marketplace.auth")

auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _session_out(db: Session, user: User) -> SessionOut:
    token, expires_at = create_session(db, user)
    return SessionOut(token=token, expires_at=expires_at, user=UserOut.model_validate(user))


_VERIFY_TTL_HOURS = 48
_RESET_TTL_HOURS = 1

MailDep = Annotated[EmailSender, Depends(get_mail_sender)]


def _issue_email_token(
    db: Session, mail: EmailSender, user: User, purpose: EmailTokenPurpose
) -> None:
    raw = secrets.token_urlsafe(32)
    ttl = _VERIFY_TTL_HOURS if purpose is EmailTokenPurpose.VERIFY else _RESET_TTL_HOURS
    db.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash_token(raw),
            expires_at=_now() + timedelta(hours=ttl),
        )
    )
    action = "verify" if purpose is EmailTokenPurpose.VERIFY else "password-reset/confirm"
    try:
        mail.send(
            user.email,
            "Verify your email" if purpose is EmailTokenPurpose.VERIFY else "Reset your password",
            f"Visit {settings.base_url}/{action}?token={raw}",
        )
    except Exception:
        # Delivery failure must never fail (or fingerprint) the enclosing
        # request: signup still succeeds, reset-request stays a uniform 200.
        # The token row is flushed-pending on the request session and commits
        # at request end — swallowing the send failure is what lets that
        # commit proceed; the user can re-request and the sweep expires it.
        logger.warning("email send failed to=%s purpose=%s", user.email, purpose)


@auth_router.post("/signup", response_model=SessionOut, status_code=201)
def signup(body: SignupRequest, db: _SessionDep, mail: MailDep) -> SessionOut:
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email, User.role == body.role)) is not None:
        raise HTTPException(status_code=409, detail="an account with this email and role exists")
    user = User(
        email=email,
        role=UserRole(body.role),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        # Race-safe backstop: a concurrent duplicate that beat the pre-check
        # select hits the unique(email, role) constraint here, not a 500.
        raise HTTPException(
            status_code=409, detail="an account with this email and role exists"
        ) from None
    # The domain record exists from the first moment the identity does.
    if user.role is UserRole.BUYER:
        repo.get_or_create_buyer(db, user.id)
    else:
        repo.get_or_create_seller(db, user.id)
    _issue_email_token(db, mail, user, EmailTokenPurpose.VERIFY)
    return _session_out(db, user)


@auth_router.post("/login", response_model=SessionOut)
def login(body: LoginRequest, db: _SessionDep) -> SessionOut:
    user = db.scalar(select(User).where(User.email == body.email.lower(), User.role == body.role))
    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # equalize timing; no enumeration
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return _session_out(db, user)


@auth_router.post("/logout")
def logout(
    db: _SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> dict[str, str]:
    token = credentials.credentials if credentials else ""
    # Deliberately hash-only (no expiry check): you can only delete the
    # session whose raw token you already hold, and deleting an expired row is harmless.
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    if row is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    db.delete(row)
    return {"status": "logged out"}


@auth_router.get("/me", response_model=UserOut)
def me(claims: Principal, db: _SessionDep) -> User:
    user = db.get(User, claims.sub)
    if user is None:  # session outlived the user row (deleted account)
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


@auth_router.post("/verify")
def verify_email(body: VerifyRequest, db: _SessionDep) -> dict[str, str]:
    row = db.scalar(
        select(EmailToken).where(EmailToken.token_hash == _hash_token(body.token)).with_for_update()
    )
    if (
        row is None
        or row.used_at is not None
        or row.expires_at < _now()
        or row.purpose is not EmailTokenPurpose.VERIFY
    ):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user.email_verified = True
    row.used_at = _now()
    return {"status": "verified"}


@auth_router.post("/password-reset/request")
def request_password_reset(body: ResetRequest, db: _SessionDep, mail: MailDep) -> dict[str, str]:
    user = db.scalar(select(User).where(User.email == body.email.lower(), User.role == body.role))
    if user is not None:
        _issue_email_token(db, mail, user, EmailTokenPurpose.RESET)
    # Identical response either way: no account enumeration via status code or
    # body. A residual timing delta remains — the exists-branch does strictly
    # more work (an insert plus a send attempt) than the ghost branch — and is
    # accepted at pilot grade alongside the documented no-rate-limiting
    # posture; closing it needs constant-time work we're not doing yet.
    return {"status": "ok"}


@auth_router.post("/password-reset/confirm")
def confirm_password_reset(body: ResetConfirmRequest, db: _SessionDep) -> dict[str, str]:
    row = db.scalar(
        select(EmailToken).where(EmailToken.token_hash == _hash_token(body.token)).with_for_update()
    )
    if (
        row is None
        or row.used_at is not None
        or row.expires_at < _now()
        or row.purpose is not EmailTokenPurpose.RESET
    ):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user.password_hash = hash_password(body.new_password)
    row.used_at = _now()
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))  # revoke everything
    # Consume every other outstanding reset token too — a successful reset
    # must leave no live reset token behind.
    db.execute(
        delete(EmailToken).where(
            EmailToken.user_id == user.id, EmailToken.purpose == EmailTokenPurpose.RESET
        )
    )
    return {"status": "password reset"}


def bootstrap_admin() -> None:
    """Seed the admin account from settings at startup. Empty settings -> no
    admin (logged), which is fine for tests and the bare template."""
    if not (settings.admin_email and settings.admin_password):
        logger.info("ADMIN_EMAIL/ADMIN_PASSWORD unset; no admin account seeded")
        return
    email = settings.admin_email.lower()
    with SessionLocal() as db:
        exists = db.scalar(select(User).where(User.email == email, User.role == UserRole.ADMIN))
        if exists is None:
            db.add(
                User(
                    email=email,
                    role=UserRole.ADMIN,
                    password_hash=hash_password(settings.admin_password),
                    display_name="admin",
                )
            )
            db.commit()
            logger.info("admin account seeded for %s", email)
        elif not verify_password(settings.admin_password, exists.password_hash):
            # A changed ADMIN_PASSWORD rotates the credential on restart; old
            # sessions are revoked so the previous password grants nothing.
            exists.password_hash = hash_password(settings.admin_password)
            db.execute(delete(AuthSession).where(AuthSession.user_id == exists.id))
            db.commit()
            logger.info("admin password rotated for %s", email)
