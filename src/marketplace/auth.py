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

from fastapi import APIRouter, Depends, Header, HTTPException
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import repo
from .db import SessionLocal, get_session
from .entities import AuthSession, User
from .models import LoginRequest, SessionOut, SignupRequest, UserOut, UserRole
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


def _principal(session: _SessionDep, authorization: Annotated[str, Header()] = "") -> Claims:
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


@auth_router.post("/signup", response_model=SessionOut, status_code=201)
def signup(body: SignupRequest, db: _SessionDep) -> SessionOut:
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
    db.flush()
    # The domain record exists from the first moment the identity does.
    if user.role is UserRole.BUYER:
        repo.get_or_create_buyer(db, user.id)
    else:
        repo.get_or_create_seller(db, user.id)
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
def logout(db: _SessionDep, authorization: Annotated[str, Header()] = "") -> dict[str, str]:
    _, _, token = authorization.partition(" ")
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
