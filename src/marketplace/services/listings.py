"""Listings: the seller's own create/update/list, and the admin listing and
takedown. `active` is seller-owned and `hidden` admin-owned; no function here
lets one side touch the other's flag, and listings never carry a price.
Public visibility is `marketplace.catalog`'s job, not this module's."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import repo
from ..entities import Listing, ServiceType
from ..errors import ConflictError, NotFoundError
from ..models import ListingCreate, ListingUpdate
from ..repo import audit
from .common import paginate as _paginate
from .common import require_active as _require_active


def create_listing(session: Session, seller_id: str, body: ListingCreate) -> Listing:
    _require_active(session, seller_id)
    if session.get(ServiceType, body.service_type_id) is None:
        raise NotFoundError("unknown service_type_id")
    seller = repo.get_or_create_seller(session, seller_id)
    if not seller.payments_ready:
        raise ConflictError("onboard payments before listing")
    listing = Listing(
        seller_id=seller_id,
        service_type_id=body.service_type_id,
        title=body.title,
        description=body.description,
        photo_urls=body.photo_urls,
    )
    session.add(listing)
    session.flush()
    return listing


def my_listings(session: Session, seller_id: str) -> list[Listing]:
    rows = session.scalars(
        select(Listing).where(Listing.seller_id == seller_id).order_by(Listing.created_at.desc())
    ).all()
    return list(rows)


def update_listing(
    session: Session, seller_id: str, listing_id: UUID, body: ListingUpdate
) -> Listing:
    _require_active(session, seller_id)
    listing = session.get(Listing, listing_id)
    if listing is None or listing.seller_id != seller_id:
        raise NotFoundError("listing not found")
    for field in ("title", "description", "photo_urls", "active"):
        value = getattr(body, field)
        if value is not None:
            setattr(listing, field, value)
    session.flush()
    return listing


def admin_list_listings(session: Session, limit: int, offset: int) -> list[Listing]:
    stmt = select(Listing).order_by(Listing.created_at.desc(), Listing.id.desc())
    return _paginate(session, stmt, limit, offset)


def hide_listing(session: Session, admin_id: str, listing_id: UUID) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise NotFoundError("listing not found")
    listing.hidden = True
    audit(session, admin_id, "hide_listing", str(listing_id), {})
    session.flush()
    return listing


def unhide_listing(session: Session, admin_id: str, listing_id: UUID) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise NotFoundError("listing not found")
    listing.hidden = False
    audit(session, admin_id, "unhide_listing", str(listing_id), {})
    session.flush()
    return listing
