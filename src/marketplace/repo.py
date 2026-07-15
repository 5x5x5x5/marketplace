"""Data-access helpers over a SQLAlchemy Session.

Plain functions (no repository interface — one implementation). They load the
pure `PricingConfig`/`Candidate` snapshots the pricing core needs, and answer the
counting queries the lifecycle needs. Counts are simple per-row where clarity
beats a join; fine at pilot scale (ponytail: revisit if a service type ever has
thousands of live sellers).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Candidate, MarginFloor, PricingConfig, ServiceSpec
from .entities import (
    AuditLog,
    Availability,
    BuyerProfile,
    Job,
    Offer,
    Pipeline,
    PlatformConfig,
    SellerProfile,
    ServiceType,
    User,
)
from .models import JobStatus, UserStatus


def get_or_create_seller(session: Session, seller_id: str) -> SellerProfile:
    prof = session.get(SellerProfile, seller_id)
    if prof is None:
        prof = SellerProfile(id=seller_id)
        session.add(prof)
        session.flush()
    return prof


def get_or_create_buyer(session: Session, buyer_id: str) -> BuyerProfile:
    prof = session.get(BuyerProfile, buyer_id)
    if prof is None:
        prof = BuyerProfile(id=buyer_id)
        session.add(prof)
        session.flush()
    return prof


def get_platform_config(session: Session) -> PlatformConfig:
    cfg = session.get(PlatformConfig, 1)
    if cfg is None:
        cfg = PlatformConfig(id=1)
        session.add(cfg)
        session.flush()
    return cfg


def load_pricing_config(session: Session, service_type_id: str) -> PricingConfig | None:
    st = session.get(ServiceType, service_type_id)
    if st is None:
        return None
    pipe = session.get(Pipeline, service_type_id)
    pc = get_platform_config(session)
    return PricingConfig(
        service=ServiceSpec(
            id=st.id,
            base_buyer_price=st.base_buyer_price,
            base_seller_payout=st.base_seller_payout,
        ),
        buyer_pipeline=list(pipe.buyer) if pipe else [],
        seller_pipeline=list(pipe.seller) if pipe else [],
        adjuster_params=dict(pc.adjuster_params),
        margin_floor=MarginFloor(
            absolute=pc.margin_absolute,
            pct=pc.margin_pct,
            ceiling_multiplier=pc.ceiling_multiplier,
        ),
        matching_strategy=pc.matching_strategy,
    )


def available_count(session: Session, service_type_id: str) -> int:
    """Supply proxy: sellers currently offering this service type."""
    n = session.scalar(
        select(func.count())
        .select_from(Availability)
        .where(Availability.service_type_id == service_type_id)
    )
    return n or 0


def active_demand(session: Session, service_type_id: str) -> int:
    """Demand proxy: jobs still in flight for this service type."""
    n = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.service_type_id == service_type_id,
            Job.status.in_([JobStatus.PENDING, JobStatus.AWAITING_PAYMENT, JobStatus.ACCEPTED]),
        )
    )
    return n or 0


def active_job_count(session: Session, seller_id: str) -> int:
    """Jobs a seller has committed to and not yet completed — their current load.

    AWAITING_PAYMENT counts: the seller accepted; the slot is held while the
    buyer's charge settles."""
    n = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.seller_id == seller_id,
            Job.status.in_([JobStatus.ACCEPTED, JobStatus.AWAITING_PAYMENT]),
        )
    )
    return n or 0


def eligible_candidates(
    session: Session, service_type_id: str, exclude: set[str]
) -> list[Candidate]:
    """Available sellers with spare capacity, minus `exclude` (already offered/declined)."""
    avails = session.scalars(
        select(Availability).where(Availability.service_type_id == service_type_id)
    ).all()
    if not avails:
        return []
    suspended = set(
        session.scalars(
            select(User.id).where(
                User.id.in_([a.seller_id for a in avails]),
                User.status == UserStatus.SUSPENDED,
            )
        ).all()
    )
    out: list[Candidate] = []
    for a in avails:
        if a.seller_id in exclude or a.seller_id in suspended:
            continue
        prof = get_or_create_seller(session, a.seller_id)
        if not prof.payments_ready:
            continue  # can't be paid → can't be offered work
        cand = Candidate(
            seller_id=a.seller_id,
            tier=prof.tier,
            rating=prof.rating,
            completed_jobs=prof.completed_jobs,
            available_since=a.since,
            active_jobs=active_job_count(session, a.seller_id),
            capacity=prof.capacity,
        )
        if cand.has_capacity:
            out.append(cand)
    return out


def sellers_seen_for_job(session: Session, job_id: UUID) -> set[str]:
    """Sellers who already had an offer for this job (so re-match skips them)."""
    rows = session.scalars(select(Offer.seller_id).where(Offer.job_id == job_id)).all()
    return set(rows)


def audit(session: Session, actor: str, action: str, target: str, detail: dict[str, Any]) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, detail=detail))
