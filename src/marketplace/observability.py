"""Request-boundary observability: logging config, request ids, access log,
error envelope.

One middleware owns the whole boundary: it stamps a request id into a
contextvar (so every app log line carries it), emits exactly one access-log
line per request, and converts any escaped exception into a clean 500
envelope — traceback to the log, never the response. A separate
@app.exception_handler would run OUTSIDE user middleware (Starlette's
ServerErrorMiddleware), losing the header and the contextvar: keeping the
envelope here is load-bearing, not style.

Access lines never carry headers, bodies, or query strings — tokens ride in
headers and reset tokens in bodies. Method, path, status, duration, id. Only.
"""

import json
import logging
import logging.config
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .settings import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_MAX_REQUEST_ID = 64
_ACCESS_FIELDS = ("method", "path", "status", "duration_ms")

access_logger = logging.getLogger("marketplace.access")
error_logger = logging.getLogger("marketplace")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key in _ACCESS_FIELDS:
            value = record.__dict__.get(key)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging() -> None:
    """Process-wide logging: one handler, one format, uvicorn folded in.

    Idempotent — dictConfig replaces handlers wholesale. App loggers keep
    propagating to root (caplog and tests rely on it); only uvicorn's own
    loggers are detached, and its access line is silenced in favor of ours.
    """
    plain = settings.log_format == "plain"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "plain": {
                    "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
                },
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "plain" if plain else "json",
                    "filters": ["request_id"],
                }
            },
            "root": {"handlers": ["stderr"], "level": "INFO"},
            "loggers": {
                "uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            },
        }
    )


def _clean_request_id(raw: str | None) -> str:
    if raw and len(raw) <= _MAX_REQUEST_ID and raw.isascii() and raw.isprintable():
        return raw
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Request id + access log + error envelope. Mount OUTERMOST (add last)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _clean_request_id(Headers(scope=scope).get("x-request-id"))
        request_id_var.set(rid)
        method = str(scope.get("method", "-"))
        path = str(scope["path"])
        start = time.monotonic()
        status = 0
        started = False

        async def send_with_header(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = int(message["status"])
                message.setdefault("headers", []).append((b"x-request-id", rid.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            error_logger.exception("unhandled error on %s %s", method, path)
            status = 500
            if not started:
                response = JSONResponse(
                    {"detail": "internal error", "request_id": rid},
                    status_code=500,
                    headers={"x-request-id": rid},
                )
                await response(scope, receive, send)
            # response already streaming: nothing safe to send; the log has it
        finally:
            if path != "/healthz":
                access_logger.info(
                    "%s %s %s",
                    method,
                    path,
                    status,
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.monotonic() - start) * 1000, 1),
                    },
                )
