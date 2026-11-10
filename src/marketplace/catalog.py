"""Shared catalog read logic — one source of truth for the JSON API and the
HTML pages.

Moved verbatim out of api.py (phase-2 pages foundation): visibility rules
for the public catalog must never drift between `/v1/catalog/*` and the
server-rendered browse pages. No logic changes versus the original
api.py functions — see tests/test_catalog.py. The `/v1/catalog/*` reads
themselves (`list_listings`, `get_listing`, `get_seller_profile`) live at
the bottom of this module; the router only parses and returns."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Row, Select, select
from sqlalchemy.orm import Session

from . import repo
from .entities import Listing, SellerProfile, User
from .errors import NotFoundError
from .models import (
    CatalogListingOut,
    CatalogSellerOut,
    CatalogSellerProfileOut,
    Side,
    UserStatus,
    to_money,
)
from .pricing import PricingContext, run_pipeline


def indicative_buyer_price(session: Session, service_type_id: str) -> Decimal | None:
    """Catalog 'from' price: the buyer pipeline at this instant, pre-floor.
    Mirrors create_quote's price computation (services/booking.py) — keep the two in
    sync); a real quote may floor-bump the price upward, so this is a
    floor-blind lower bound, never an exact promise."""
    cfg = repo.load_pricing_config(session, service_type_id)
    if cfg is None:
        return None
    supply = repo.available_count(session, service_type_id)
    demand = repo.active_demand(session, service_type_id) + 1
    ctx = PricingContext(
        side=Side.BUYER, buyer_completed_jobs=0, live_supply=supply, live_demand=demand
    )
    return to_money(
        run_pipeline(
            float(cfg.service.base_buyer_price), cfg.buyer_pipeline, ctx, cfg.adjuster_params
        )
    )


def listing_bookable(session: Session, listing: Listing) -> bool:
    """True iff `listing` is currently visible/bookable to the public: the
    seller's own active toggle is on, admin hasn't hidden it, the seller's
    account is ACTIVE, and the seller has completed payments onboarding.

    Single-row companion to `visible_listings_stmt` below — shared by the
    catalog's list/detail reads and the pinned-booking guards in
    `create_quote`/`create_job` (quote/job-creation time), so the two can
    never drift apart.
    """
    if not listing.active or listing.hidden:
        return False
    user = session.get(User, listing.seller_id)
    if user is None or user.status is not UserStatus.ACTIVE:
        return False
    seller = session.get(SellerProfile, listing.seller_id)
    return seller is not None and seller.payments_ready


def visible_listings_stmt() -> Select[tuple[Listing, User, SellerProfile]]:
    """Select of (Listing, User, SellerProfile) rows visible to the public
    catalog: listing active, not admin-hidden, seller account ACTIVE, seller
    payments onboarded. Row-equivalent of `listing_bookable` above.

    Task-3 interface: extend with `.where(Listing.id == ...)` /
    `.where(Listing.service_type_id == ...)` for detail/list/booking reuse —
    don't re-derive this join+filter elsewhere.
    """
    return (
        select(Listing, User, SellerProfile)
        .join(User, User.id == Listing.seller_id)
        .join(SellerProfile, SellerProfile.id == Listing.seller_id)
        .where(
            Listing.active.is_(True),
            Listing.hidden.is_(False),
            User.status == UserStatus.ACTIVE,
            SellerProfile.payments_ready.is_(True),
        )
    )


def visible_listings_page(
    limit: int, offset: int, service_type_id: str | None = None
) -> Select[tuple[Listing, User, SellerProfile]]:
    """One page of `visible_listings_stmt`, newest first, bounded in SQL.

    The `Listing.id` tie-breaker makes the order total, so LIMIT/OFFSET pages
    never skip or repeat a listing that shares a `created_at` with another.
    Every paged catalog consumer (the JSON list, the landing page) goes
    through here rather than fetching all visible rows and slicing."""
    stmt = visible_listings_stmt()
    if service_type_id is not None:
        stmt = stmt.where(Listing.service_type_id == service_type_id)
    return stmt.order_by(Listing.created_at.desc(), Listing.id.desc()).limit(limit).offset(offset)


def seller_publicly_visible(user: User | None, seller: SellerProfile | None) -> bool:
    """Whether a seller's own profile/lookup endpoint should resolve at all:
    account exists, ACTIVE, and payments onboarded. Single-seller equivalent
    of `visible_listings_stmt`'s row filter above — same rule, shared here so
    the catalog API's `/v1/catalog/sellers/{id}` and the pages router's
    `/sellers/{id}` can never drift on what counts as a visible seller."""
    return (
        user is not None
        and seller is not None
        and user.status is UserStatus.ACTIVE
        and seller.payments_ready
    )


def catalog_seller_out(user: User, seller: SellerProfile) -> CatalogSellerOut:
    return CatalogSellerOut(
        seller_id=user.id,
        display_name=user.display_name,
        rating=seller.rating,
        rating_count=seller.rating_count,
    )


def catalog_listing_out(
    listing: Listing, user: User, seller: SellerProfile, price: Decimal
) -> CatalogListingOut:
    return CatalogListingOut(
        id=listing.id,
        title=listing.title,
        description=listing.description,
        photo_urls=listing.photo_urls,
        service_type_id=listing.service_type_id,
        price_from=price,
        seller=catalog_seller_out(user, seller),
    )


def catalog_listings_for_rows(
    session: Session, rows: Sequence[Row[tuple[Listing, User, SellerProfile]]]
) -> list[CatalogListingOut]:
    """Map visible (Listing, User, SellerProfile) rows to catalog cards,
    memoizing `indicative_buyer_price` per service_type_id (one pipeline
    evaluation per type per response, not per listing). A listing whose
    service type no longer resolves (should not happen — service types are
    never deleted) is silently omitted rather than crashing the response."""
    price_cache: dict[str, Decimal | None] = {}
    out: list[CatalogListingOut] = []
    for listing, user, seller in rows:
        if listing.service_type_id not in price_cache:
            price_cache[listing.service_type_id] = indicative_buyer_price(
                session, listing.service_type_id
            )
        price = price_cache[listing.service_type_id]
        if price is None:
            continue
        out.append(catalog_listing_out(listing, user, seller, price))
    return out


# ---------- /v1/catalog/* reads (moved from the api.py catalog router) ----------


def list_listings(
    session: Session, service_type_id: str | None, limit: int, offset: int
) -> list[CatalogListingOut]:
    rows = session.execute(visible_listings_page(limit, offset, service_type_id)).all()
    return catalog_listings_for_rows(session, rows)


def get_listing(session: Session, listing_id: UUID) -> CatalogListingOut:
    row = session.execute(visible_listings_stmt().where(Listing.id == listing_id)).first()
    if row is None:
        raise NotFoundError("listing not found")
    listing, user, seller = row
    price = indicative_buyer_price(session, listing.service_type_id)
    if price is None:
        raise NotFoundError("listing not found")
    return catalog_listing_out(listing, user, seller, price)


def get_seller_profile(session: Session, seller_id: str) -> CatalogSellerProfileOut:
    user = session.get(User, seller_id)
    seller = session.get(SellerProfile, seller_id)
    if not seller_publicly_visible(user, seller):
        raise NotFoundError("seller not found")
    assert user is not None
    assert seller is not None
    rows = session.execute(visible_listings_stmt().where(Listing.seller_id == seller_id)).all()
    return CatalogSellerProfileOut(
        seller_id=user.id,
        display_name=user.display_name,
        rating=seller.rating,
        rating_count=seller.rating_count,
        completed_jobs=seller.completed_jobs,
        listings=catalog_listings_for_rows(session, rows),
    )
