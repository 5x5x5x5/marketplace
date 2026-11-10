"""Client-facing idempotency: optional Idempotency-Key header on POSTs.

The first response is stored per (principal, key) and replayed byte-for-byte
on repeats, except 401/403 (auth/authorization-state answers, not operation
outcomes — a suspended-then-reinstated principal must not replay a stale
403 forever) and 5xx. The same key on a different path is a 409. Uses its
own short-lived DB sessions, separate from the request's.

The downstream app's response is buffered in memory and only forwarded to
the client AFTER the IdempotencyRecord commit (finding F2b): the record must
be durable before the client can possibly retry, or an immediate same-key
retry can re-execute instead of replaying. A failure storing the record
(anything short of the IntegrityError-duplicate case below) is logged and
swallowed rather than raised: the domain work already committed, so a missed
replay record beats turning a successful operation into a 500.

ponytail: the store races on truly concurrent duplicates — both execute, the
unique constraint drops one record, and the DB row locks downstream already
make the duplicate call safe. A reserve-then-execute two-phase insert is the
upgrade if exactly-once matters more than simplicity.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import peek_principal
from .db import SessionLocal
from .entities import IdempotencyRecord

logger = logging.getLogger("marketplace.idempotency")

MAX_KEY_LENGTH = 200

# The cookie name pages.py's session_token Cookie() parameter binds to
# (auth.py's _resolve_principal/optional_claims resolve the same cookie by
# that parameter name). Not imported from pages — pages -> auth is the only
# allowed import direction (see CLAUDE.md); this module reads the raw
# "cookie" header itself instead.
_SESSION_COOKIE = "session_token"

# Page routes whose POSTs must never be captured for replay: /signup and
# /login (pages.py) issue a *raw* session token via Set-Cookie and answer
# with a 303 redirect; a stored replay would resend neither the Location
# header nor Set-Cookie, breaking the redirect on a second identical
# request. /logout likewise redirects and clears a cookie. Kept as an
# explicit list (not "any path outside /v1/") so a future non-API page POST
# is never silently exempted just by living outside /v1 — each addition
# here has to be a deliberate call.
_PAGE_AUTH_PATHS = frozenset({"/signup", "/login", "/logout"})


def _lookup(
    authorization: str | None, session_token: str | None, key: str
) -> tuple[str | None, IdempotencyRecord | None]:
    with SessionLocal() as session:
        principal = peek_principal(session, authorization, session_token)
        if principal is None:
            return None, None
        row = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.principal == principal, IdempotencyRecord.key == key
            )
        )
        return principal, row


def _store(record: IdempotencyRecord) -> None:
    with SessionLocal() as session:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()  # concurrent duplicate won the insert; fine


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        key = headers.get("idempotency-key")
        if key is None:
            await self.app(scope, receive, send)
            return
        if len(key) > MAX_KEY_LENGTH:
            response: Response = JSONResponse(
                {"detail": "Idempotency-Key too long"}, status_code=422
            )
            await response(scope, receive, send)
            return
        path = str(scope["path"])
        if path.startswith("/v1/auth/") or path in _PAGE_AUTH_PATHS:
            # /v1/auth/*: responses carry raw bearer tokens; they must never
            # be captured into idempotency_keys (sha256-at-rest guarantee).
            # Login is naturally repeatable and signup-replay correctly
            # 409s, so idempotency semantics aren't wanted here anyway.
            # /signup, /login, /logout (pages.py): see _PAGE_AUTH_PATHS above.
            await self.app(scope, receive, send)
            return
        cookie_header = headers.get("cookie")
        session_token = cookie_parser(cookie_header).get(_SESSION_COOKIE) if cookie_header else None
        # threadpool: this is a sync DB checkout inside an async middleware;
        # on the event loop, a wait for a pool connection would stall every
        # in-flight request, not just this one.
        principal, row = await run_in_threadpool(
            _lookup, headers.get("authorization"), session_token, key
        )
        if principal is None:
            # Neither the header nor the session_token cookie resolved a
            # principal; auth 401s downstream with the real error.
            await self.app(scope, receive, send)
            return
        if row is not None:
            if row.path != path:
                replay: Response = JSONResponse(
                    {"detail": "Idempotency-Key was already used for a different request"},
                    status_code=409,
                )
            else:
                replay = Response(
                    content=row.response_body,
                    status_code=row.response_status,
                    media_type="application/json",
                )
            await replay(scope, receive, send)
            return

        captured_status = 500
        captured_body = b""
        buffered: list[Message] = []

        async def buffer_send(message: Message) -> None:
            nonlocal captured_status, captured_body
            if message["type"] == "http.response.start":
                captured_status = int(message["status"])
            elif message["type"] == "http.response.body":
                captured_body += bytes(message.get("body", b""))
            buffered.append(message)

        await self.app(scope, receive, buffer_send)

        # int(...) re-widens the type: pyright can't see that buffer_send (an
        # opaque callback passed to self.app) is what actually reassigns
        # captured_status, so without this it stays narrowed to Literal[500]
        # and flags the auth-status exclusion below as an always-true no-op.
        status = int(captured_status)
        if status < 500 and status != 401 and status != 403:
            try:
                record = IdempotencyRecord(
                    principal=principal,
                    key=key,
                    path=path,
                    response_status=status,
                    response_body=captured_body.decode("utf-8", errors="replace"),
                )
                await run_in_threadpool(_store, record)
            except Exception:
                # The domain work is already committed (CommitRoute ran inside
                # self.app above) — a missed replay record beats failing a
                # successful operation.
                logger.warning(
                    "idempotency store failed for %s %s; response sent without replay record",
                    principal,
                    path,
                )
        for message in buffered:
            await send(message)
