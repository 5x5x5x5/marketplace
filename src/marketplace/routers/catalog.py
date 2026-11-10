"""Public catalog (`/v1/catalog/...`), unauthenticated. Visibility, pricing
and the view builders are all `marketplace.catalog`; these routes only parse
and return."""

from uuid import UUID

from fastapi import APIRouter

from .. import catalog
from ..db import CommitRoute
from ..models import CatalogListingOut, CatalogSellerProfileOut
from .deps import Limit, Offset, SessionDep

catalog_router = APIRouter(prefix="/v1/catalog", tags=["catalog"], route_class=CommitRoute)


@catalog_router.get("/listings", response_model=list[CatalogListingOut])
def catalog_list_listings(
    session: SessionDep,
    service_type_id: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[CatalogListingOut]:
    return catalog.list_listings(session, service_type_id, limit, offset)


@catalog_router.get("/listings/{listing_id}", response_model=CatalogListingOut)
def catalog_get_listing(listing_id: UUID, session: SessionDep) -> CatalogListingOut:
    return catalog.get_listing(session, listing_id)


@catalog_router.get("/sellers/{seller_id}", response_model=CatalogSellerProfileOut)
def catalog_get_seller(seller_id: str, session: SessionDep) -> CatalogSellerProfileOut:
    return catalog.get_seller_profile(session, seller_id)
