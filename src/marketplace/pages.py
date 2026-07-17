"""Server-rendered pages: the browse storefront (phase 2 foundation).

Import direction: pages -> (db, auth, catalog, entities, models). NEVER
import api (api includes this router)."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import catalog
from .db import CommitRoute, get_session
from .entities import Listing, SellerProfile, User
from .models import CatalogSellerProfileOut, UserStatus

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


def _not_found(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"claims": None, "status_code": 404, "message": message},
        status_code=404,
    )


@pages_router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(request: Request, session: _SessionDep, listing_id: UUID) -> HTMLResponse:
    row = session.execute(catalog.visible_listings_stmt().where(Listing.id == listing_id)).first()
    if row is None:
        return _not_found(request, "This listing isn't available.")
    listing, user, seller = row
    price = catalog.indicative_buyer_price(session, listing.service_type_id)
    if price is None:
        return _not_found(request, "This listing isn't available.")
    listing_out = catalog.catalog_listing_out(listing, user, seller, price)
    return templates.TemplateResponse(
        request, "listing.html", {"claims": None, "listing": listing_out}
    )


@pages_router.get("/sellers/{seller_id}", response_class=HTMLResponse)
def seller_profile(request: Request, session: _SessionDep, seller_id: str) -> HTMLResponse:
    user = session.get(User, seller_id)
    seller = session.get(SellerProfile, seller_id)
    if (
        user is None
        or seller is None
        or user.status is not UserStatus.ACTIVE
        or not seller.payments_ready
    ):
        return _not_found(request, "This seller isn't available.")
    rows = session.execute(
        catalog.visible_listings_stmt().where(Listing.seller_id == seller_id)
    ).all()
    listings = catalog.catalog_listings_for_rows(session, rows)
    seller_summary = catalog.catalog_seller_out(user, seller)
    seller_out = CatalogSellerProfileOut(
        **seller_summary.model_dump(),
        completed_jobs=seller.completed_jobs,
        listings=listings,
    )
    return templates.TemplateResponse(
        request, "seller.html", {"claims": None, "seller": seller_out}
    )
