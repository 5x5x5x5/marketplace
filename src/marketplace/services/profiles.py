"""Buyer and seller profiles, seller availability, and the admin views of
both."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import repo
from ..entities import Availability, BuyerProfile, SellerProfile, ServiceType
from ..errors import NotFoundError
from ..models import AdminSellerBody, AvailabilityRequest, SellerProfileUpdate
from ..repo import audit
from .common import paginate as _paginate
from .common import require_active as _require_active


def get_buyer_profile(session: Session, buyer_id: str) -> BuyerProfile:
    return repo.get_or_create_buyer(session, buyer_id)


def update_profile(session: Session, seller_id: str, body: SellerProfileUpdate) -> SellerProfile:
    seller = repo.get_or_create_seller(session, seller_id)
    seller.capacity = body.capacity
    session.flush()
    return seller


def get_profile(session: Session, seller_id: str) -> SellerProfile:
    return repo.get_or_create_seller(session, seller_id)


def post_availability(session: Session, seller_id: str, req: AvailabilityRequest) -> None:
    _require_active(session, seller_id)
    if session.get(ServiceType, req.service_type_id) is None:
        raise NotFoundError("unknown service_type_id")
    repo.get_or_create_seller(session, seller_id)
    existing = session.scalar(
        select(Availability).where(
            Availability.seller_id == seller_id,
            Availability.service_type_id == req.service_type_id,
        )
    )
    if existing is None:
        session.add(Availability(seller_id=seller_id, service_type_id=req.service_type_id))


def delete_availability(session: Session, seller_id: str, service_type_id: str) -> None:
    row = session.scalar(
        select(Availability).where(
            Availability.seller_id == seller_id,
            Availability.service_type_id == service_type_id,
        )
    )
    if row is None:
        raise NotFoundError("availability not found")
    session.delete(row)


def admin_update_seller(
    session: Session, admin_id: str, seller_id: str, body: AdminSellerBody
) -> SellerProfile:
    seller = repo.get_or_create_seller(session, seller_id)
    if body.tier is not None:
        seller.tier = body.tier
    if body.capacity is not None:
        seller.capacity = body.capacity
    audit(
        session,
        admin_id,
        "update_seller",
        seller_id,
        body.model_dump(mode="json", exclude_none=True),
    )
    session.flush()
    return seller


def admin_list_buyers(session: Session, limit: int, offset: int) -> list[BuyerProfile]:
    return _paginate(session, select(BuyerProfile).order_by(BuyerProfile.id), limit, offset)
