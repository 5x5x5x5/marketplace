"""Session-bound CSRF tokens for authenticated page forms.

token = sha256("marketplace-csrf-v1\\0" || raw session token), hex.

- Bound to the session: derived from the raw token in the `session_token`
  cookie, so it changes with every login and dies with the session row.
  No stored state, no second session mechanism.
- No server secret: the raw session token is already a 256-bit secret only
  the browser holds (the cookie is HttpOnly).
- Distinct from `auth_sessions.token_hash` (= sha256(raw)): a database
  reader cannot compute it, and a page reader learns nothing about the raw
  token or its stored hash. A fixed prefix, not a suffix, so it is never a
  length extension of anything stored.
- Compared in constant time; missing cookie, missing field or mismatch fails
  closed with a 403 before the handler runs.
"""

import hashlib
import hmac
from typing import Annotated

from fastapi import Cookie, Depends, Form

from .interrupts import PageForbidden

_DOMAIN = b"marketplace-csrf-v1\x00"
_MESSAGE = (
    "This form has expired or didn't come from this site. Go back, reload the page and try again."
)

# Unauthenticated forms: there is no session to bind a token to. Adding a
# path here is a security decision (tests/test_architecture.py pins it).
CSRF_EXEMPT_POSTS = frozenset(
    {"/signup", "/login", "/verify", "/password-reset/request", "/password-reset/confirm"}
)


def token_for(raw_session_token: str) -> str:
    return hashlib.sha256(_DOMAIN + raw_session_token.encode()).hexdigest()


def require_csrf(
    session_token: Annotated[str | None, Cookie()] = None,
    csrf_token: Annotated[str | None, Form()] = None,
) -> None:
    if not session_token or not csrf_token:
        raise PageForbidden(_MESSAGE)
    if not hmac.compare_digest(token_for(session_token).encode(), csrf_token.encode()):
        raise PageForbidden(_MESSAGE)


CSRF = Depends(require_csrf)
