"""The public storefront: catalog grid, listing detail, seller profile.
Catalog visibility and "from" prices come from `catalog.py` only."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import catalog
from ..db import CommitRoute
from ..entities import Listing, SellerProfile, User
from ..models import CatalogSellerProfileOut
from .common import PAGE_SIZE, OptionalClaims, SessionDep, error_page, nav_context, render

router = APIRouter(route_class=CommitRoute, tags=["pages"])


@router.get("/", response_class=HTMLResponse)
def landing(
    request: Request,
    session: SessionDep,
    claims: OptionalClaims,
    service_type_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    offset = (page - 1) * PAGE_SIZE
    # one extra row tells us whether a next page exists
    stmt = catalog.visible_listings_page(PAGE_SIZE + 1, offset, service_type_id)
    rows = session.execute(stmt).all()
    has_next = len(rows) > PAGE_SIZE
    listings = catalog.catalog_listings_for_rows(session, rows[:PAGE_SIZE])
    return render(
        request,
        "index.html",
        {
            **nav_context(session, claims),
            "listings": listings,
            "service_type_id": service_type_id,
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(
    request: Request, session: SessionDep, claims: OptionalClaims, listing_id: UUID
) -> HTMLResponse:
    row = session.execute(catalog.visible_listings_stmt().where(Listing.id == listing_id)).first()
    if row is None:
        return error_page(request, session, claims, 404, "This listing isn't available.")
    listing, user, seller = row
    price = catalog.indicative_buyer_price(session, listing.service_type_id)
    if price is None:
        return error_page(request, session, claims, 404, "This listing isn't available.")
    listing_out = catalog.catalog_listing_out(listing, user, seller, price)
    return render(
        request,
        "listing.html",
        {**nav_context(session, claims), "listing": listing_out},
    )


@router.get("/sellers/{seller_id}", response_class=HTMLResponse)
def seller_profile(
    request: Request, session: SessionDep, claims: OptionalClaims, seller_id: str
) -> HTMLResponse:
    user = session.get(User, seller_id)
    seller = session.get(SellerProfile, seller_id)
    if not catalog.seller_publicly_visible(user, seller):
        return error_page(request, session, claims, 404, "This seller isn't available.")
    assert user is not None
    assert seller is not None
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
    return render(
        request,
        "seller.html",
        {**nav_context(session, claims), "seller": seller_out},
    )
