"""App assembly: the FastAPI app, its middleware and lifespan, the one
`DomainError` -> HTTP mapping, router mounting, and the import-time check
that every route commits before its response is sent.

Nothing else lives here. The JSON routers are in `routers/`, all under
``/v1`` and role-scoped so paths never collide:
  /v1/...          buyer-facing                        routers/buyer.py
  /v1/seller/...   seller-facing                       routers/seller.py
  /v1/admin/...    operator-facing (both sides' numbers) routers/admin.py
  /v1/catalog/...  public browse                       routers/catalog.py
  /v1/payments/... provider webhook                    routers/payments.py
plus `/v1/reports`, `/v1/notification-preferences`, `auth.py`'s auth routes
and `pages.py`'s server-rendered pages. Domain logic is in `services/`;
nothing below `api` imports it (`marketplace.api:app` is the import path
for uvicorn, tests and scripts).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from .auth import auth_router, bootstrap_admin
from .db import CommitRoute, init_db
from .errors import DomainError
from .idempotency import IdempotencyMiddleware
from .observability import BodySizeLimitMiddleware, RequestIdMiddleware, configure_logging
from .pages import pages_router
from .routers.admin import admin_router
from .routers.buyer import buyer_router
from .routers.catalog import catalog_router
from .routers.payments import payments_router
from .routers.preferences import prefs_router
from .routers.reports import reports_router
from .routers.seller import seller_router
from .services import maintenance
from .settings import settings

logger = logging.getLogger("marketplace")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Re-assert at startup too: the module-level check fails open for any
    # router registered after it (e.g. a conditional include below it).
    _assert_commit_routes(app)
    # Create tables for SQLite/dev. Production applies Alembic migrations instead.
    if settings.database_url.startswith("sqlite"):
        init_db()
    bootstrap_admin()
    logger.info("marketplace starting (db=%s)", settings.database_url.split("://", 1)[0])
    if not settings.maintenance_enabled:
        logger.info("maintenance loop disabled (MAINTENANCE_ENABLED=false) on this instance")
        yield
        return
    loop_task = asyncio.create_task(maintenance.maintenance_loop())
    yield
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task


def _add_hardening_middleware(app: FastAPI) -> None:
    """Settings-driven: CORS only when origins are configured; TrustedHost
    defaults open (*) until narrowed; body cap always on."""
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(BodySizeLimitMiddleware)


configure_logging()
app = FastAPI(title="Marketplace", version="1.0.0", lifespan=_lifespan)
app.add_middleware(IdempotencyMiddleware)
_add_hardening_middleware(app)
app.add_middleware(RequestIdMiddleware)  # outermost, unchanged


async def _domain_error_response(request: Request, exc: Exception) -> Response:
    """The one place a service-layer error becomes HTTP: rendered by FastAPI's
    own HTTPException handler, so status, `detail` body and headers are exactly
    what the route would have produced by raising HTTPException itself."""
    assert isinstance(exc, DomainError)  # registered for DomainError only
    return await http_exception_handler(
        request, HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers)
    )


app.add_exception_handler(DomainError, _domain_error_response)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(buyer_router)
app.include_router(seller_router)
app.include_router(admin_router)
app.include_router(reports_router)
app.include_router(prefs_router)
app.include_router(payments_router)
app.include_router(catalog_router)
app.include_router(pages_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


_COMMIT_EXEMPT = {"/healthz"}


def _assert_commit_routes(app: FastAPI) -> None:
    """Every route in the app must commit-before-response (finding F2); a
    plain APIRoute would silently regress to commit-in-teardown. Widened
    (pages foundation) from a /v1-only check to app-wide: the pages layer
    (pages.py) mutates no state today, but a future page/form route missing
    route_class=CommitRoute must fail the same way an API route would.
    Mounts (e.g. the /static StaticFiles app) aren't APIRoute instances and
    are skipped; /healthz is the sole deliberate exemption."""
    rogue = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and not isinstance(route, CommitRoute)
        and route.path not in _COMMIT_EXEMPT
    ]
    if rogue:
        raise RuntimeError(f"routes missing route_class=CommitRoute (finding F2): {rogue}")


_assert_commit_routes(app)
