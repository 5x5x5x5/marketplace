"""Data-access helpers over a SQLAlchemy Session.

Plain functions (no repository interface — one implementation). They load the
pure `PricingConfig`/`Candidate` snapshots the pricing core needs, and answer the
counting queries the lifecycle needs. Loads that run per candidate seller are
batched (a fixed number of queries per call, not one per seller) because they
sit on the quote and re-match paths.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Candidate, FeeConfig, MarginFloor, PricingConfig, ServiceSpec
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


def fee_config(session: Session) -> FeeConfig:
    pc = get_platform_config(session)
    return FeeConfig(pct=pc.fee_pct, fixed=pc.fee_fixed)


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
        fees=FeeConfig(pct=pc.fee_pct, fixed=pc.fee_fixed),
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


def active_job_counts(session: Session, seller_ids: list[str]) -> dict[str, int]:
    """`active_job_count` for many sellers in one grouped query (absent = 0)."""
    if not seller_ids:
        return {}
    rows = session.execute(
        select(Job.seller_id, func.count())
        .where(
            Job.seller_id.in_(seller_ids),
            Job.status.in_([JobStatus.ACCEPTED, JobStatus.AWAITING_PAYMENT]),
        )
        .group_by(Job.seller_id)
    ).all()
    return {seller_id: int(n) for seller_id, n in rows if seller_id is not None}


def eligible_candidates(
    session: Session, service_type_id: str, exclude: set[str]
) -> list[Candidate]:
    """Available sellers with spare capacity, minus `exclude` (already offered/declined).

    A fixed number of queries however many sellers are available: one for
    availability joined to payments-ready profiles, one for suspensions, one
    grouped count for current load. A seller with no profile can't have been
    onboarded, so the inner join drops them exactly as `payments_ready` would."""
    rows = session.execute(
        select(Availability, SellerProfile)
        .join(SellerProfile, SellerProfile.id == Availability.seller_id)
        .where(
            Availability.service_type_id == service_type_id,
            SellerProfile.payments_ready.is_(True),  # can't be paid -> can't be offered work
        )
        .order_by(Availability.since, Availability.seller_id)
    ).all()
    rows = [(a, prof) for a, prof in rows if a.seller_id not in exclude]
    if not rows:
        return []
    ids = [a.seller_id for a, _ in rows]
    suspended = set(
        session.scalars(
            select(User.id).where(User.id.in_(ids), User.status == UserStatus.SUSPENDED)
        ).all()
    )
    loads = active_job_counts(session, ids)
    out: list[Candidate] = []
    for a, prof in rows:
        if a.seller_id in suspended:
            continue
        cand = Candidate(
            seller_id=a.seller_id,
            tier=prof.tier,
            rating=prof.rating,
            completed_jobs=prof.completed_jobs,
            available_since=a.since,
            active_jobs=loads.get(a.seller_id, 0),
            capacity=prof.capacity,
        )
        if cand.has_capacity:
            out.append(cand)
    return out


def candidate_for_seller(session: Session, seller_id: str) -> Candidate | None:
    """Build a `Candidate` for one specific seller, regardless of availability.

    The pinned floor probe (and pinned offer creation) need this seller's
    payout math even when the question isn't "is anyone available" but "is
    THIS seller available" — that's checked separately, by the caller, via
    `Availability`/`_listing_bookable`. Returns None if the seller has no
    profile (mirrors `eligible_candidates`'s Candidate construction)."""
    prof = session.get(SellerProfile, seller_id)
    if prof is None:
        return None
    return Candidate(
        seller_id=seller_id,
        tier=prof.tier,
        rating=prof.rating,
        completed_jobs=prof.completed_jobs,
        available_since=datetime.now(UTC),
        active_jobs=active_job_count(session, seller_id),
        capacity=prof.capacity,
    )


def sellers_seen_for_job(session: Session, job_id: UUID) -> set[str]:
    """Sellers who already had an offer for this job (so re-match skips them)."""
    rows = session.scalars(select(Offer.seller_id).where(Offer.job_id == job_id)).all()
    return set(rows)


def audit(session: Session, actor: str, action: str, target: str, detail: dict[str, Any]) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, detail=detail))
