"""Client-facing idempotency: optional Idempotency-Key header on POSTs.

The first response is stored per (principal, key) and replayed byte-for-byte
on repeats, except 401/403 (auth/authorization-state answers, not operation
outcomes — a suspended-then-reinstated principal must not replay a stale
403 forever) and 5xx. The same key on a different path is a 409. Uses its
own short-lived DB sessions, separate from the request's.

ponytail: the store races on truly concurrent duplicates — both execute, the
unique constraint drops one record, and the DB row locks downstream already
make the duplicate call safe. A reserve-then-execute two-phase insert is the
upgrade if exactly-once matters more than simplicity.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import peek_principal
from .db import SessionLocal
from .entities import IdempotencyRecord

MAX_KEY_LENGTH = 200


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
        if path.startswith("/v1/auth/"):
            # Auth responses carry raw bearer tokens; they must never be
            # captured into idempotency_keys (sha256-at-rest guarantee).
            # Login is naturally repeatable and signup-replay correctly 409s,
            # so idempotency semantics aren't wanted here anyway.
            await self.app(scope, receive, send)
            return
        with SessionLocal() as session:
            principal = peek_principal(session, headers.get("authorization"))
            row = (
                None
                if principal is None
                else session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.principal == principal, IdempotencyRecord.key == key
                    )
                )
            )
        if principal is None:
            await self.app(scope, receive, send)  # auth 401s downstream with the real error
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

        async def record_send(message: Message) -> None:
            nonlocal captured_status, captured_body
            if message["type"] == "http.response.start":
                captured_status = int(message["status"])
            elif message["type"] == "http.response.body":
                captured_body += bytes(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, record_send)

        # int(...) re-widens the type: pyright can't see that record_send (an
        # opaque callback passed to self.app) is what actually reassigns
        # captured_status, so without this it stays narrowed to Literal[500]
        # and flags the auth-status exclusion below as an always-true no-op.
        status = int(captured_status)
        if status < 500 and status != 401 and status != 403:
            with SessionLocal() as session:
                session.add(
                    IdempotencyRecord(
                        principal=principal,
                        key=key,
                        path=path,
                        response_status=status,
                        response_body=captured_body.decode("utf-8", errors="replace"),
                    )
                )
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # concurrent duplicate won the insert; fine
