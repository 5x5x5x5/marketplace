"""Server-rendered pages: the browse storefront (phase 2 foundation).

Import direction: pages -> (db, auth, catalog, entities, models). NEVER
import api (api includes this router)."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import catalog
from .db import CommitRoute, get_session
from .entities import Listing

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
pages_router = APIRouter(route_class=CommitRoute, tags=["pages"])

_SessionDep = Annotated[Session, Depends(get_session)]
_PAGE_SIZE = 24


@pages_router.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    session: _SessionDep,
    service_type_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    stmt = catalog.visible_listings_stmt().order_by(Listing.created_at.desc())
    if service_type_id is not None:
        stmt = stmt.where(Listing.service_type_id == service_type_id)
    offset = (page - 1) * _PAGE_SIZE
    rows = session.execute(stmt.offset(offset).limit(_PAGE_SIZE + 1)).all()
    has_next = len(rows) > _PAGE_SIZE
    listings = catalog.catalog_listings_for_rows(session, rows[:_PAGE_SIZE])
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "claims": None,
            "listings": listings,
            "service_type_id": service_type_id,
            "page": page,
            "has_next": has_next,
        },
    )
