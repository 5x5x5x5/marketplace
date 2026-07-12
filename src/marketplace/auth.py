"""Pilot-grade authentication: HMAC-signed bearer tokens, stdlib only.

Every request carries a token asserting ``{role, sub}``; the FastAPI
dependencies below verify the signature and hand the endpoint the authenticated
subject id. Endpoints derive ``buyer_id``/``seller_id`` from that subject — never
from the request body — so identity cannot be spoofed by editing a field.

ponytail: shared-secret HMAC, no refresh/rotation/user store. This is enough to
give a pilot real identity without a database. Upgrade path once persistence
lands: fastapi-users or Supabase Auth backed by a real user table (see
ROADMAP.md).
"""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .settings import settings

Role = Literal["buyer", "seller", "admin"]

# ponytail: shared secret from settings (MARKETPLACE_SECRET), dev fallback keeps
# local/test turnkey. Rotate by changing the secret; real KMS/rotation is a
# post-pilot concern.
_SECRET = settings.marketplace_secret.encode()


class _Claims(BaseModel):
    role: Role
    sub: str = Field(min_length=1, max_length=128)
    exp: int  # unix seconds; tokens expire (closes the never-expiring-token gap)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())


def mint_token(role: str, sub: str, ttl_hours: float | None = None) -> str:
    """Issue a signed token asserting that ``sub`` acts as ``role`` until ``exp``.

    Dev/pilot helper — in production, tokens are minted at login by the real
    auth provider, not by this function. Raises on an invalid role.
    """
    ttl = settings.token_ttl_hours if ttl_hours is None else ttl_hours
    exp = int((datetime.now(UTC) + timedelta(hours=ttl)).timestamp())
    claims = _Claims.model_validate({"role": role, "sub": sub, "exp": exp})
    payload = _b64(claims.model_dump_json().encode())
    return f"{payload}.{_sign(payload)}"


def _verify(token: str) -> _Claims:
    payload, _, sig = token.partition(".")
    if not payload or not sig or not hmac.compare_digest(sig, _sign(payload)):
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        claims = _Claims.model_validate_json(_unb64(payload))
    except (ValidationError, ValueError):
        raise HTTPException(status_code=401, detail="invalid token") from None
    if claims.exp < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=401, detail="token expired")
    return claims


def _principal(authorization: Annotated[str, Header()] = "") -> _Claims:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return _verify(token)


Principal = Annotated[_Claims, Depends(_principal)]


def require_admin(claims: Principal) -> str:
    if claims.role != "admin":
        raise HTTPException(status_code=403, detail="admin credentials required")
    return claims.sub


def current_buyer(claims: Principal) -> str:
    if claims.role != "buyer":
        raise HTTPException(status_code=403, detail="buyer credentials required")
    return claims.sub


def current_seller(claims: Principal) -> str:
    if claims.role != "seller":
        raise HTTPException(status_code=403, detail="seller credentials required")
    return claims.sub
