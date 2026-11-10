"""Matching and offers: hand a job to the next eligible seller, or expire it.

Lock order on every offer path is Offer -> SellerProfile -> Job (accept,
decline and the expiry sweep all take it in that order)."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import notifications, repo
from ..entities import Job, Offer
from ..matching import STRATEGIES
from ..models import EventKind, JobStatus, OfferStatus
from ..settings import settings
from .common import now as _now


def create_offer(session: Session, job: Job, seller_id: str, payout: Any) -> None:
    expires_at = _now() + timedelta(minutes=settings.offer_ttl_minutes)
    session.add(
        Offer(
            job_id=job.id,
            service_type_id=job.service_type_id,
            seller_id=seller_id,
            seller_payout=payout,
            expires_at=expires_at,
        )
    )
    notifications.enqueue(
        session,
        EventKind.OFFER_RECEIVED,
        seller_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "seller_payout": str(payout),
            "expires_at": expires_at.isoformat(),
        },
    )


def _expire_unmatched(session: Session, job: Job) -> None:
    job.status = JobStatus.EXPIRED
    notifications.enqueue(
        session,
        EventKind.JOB_EXPIRED_BUYER,
        job.buyer_id,
        {
            "job_id": str(job.id),
            "service_type_id": job.service_type_id,
            "reason": "no seller available",
        },
    )


def match_and_offer(session: Session, job: Job) -> None:
    """Offer `job` to the next eligible seller, or mark it EXPIRED if none fit.

    Excludes any seller who already had an offer for this job (so decline/expiry
    walk down the candidate list instead of looping).
    """
    if job.pinned_seller_id is not None:
        # Browse contract: the buyer chose this seller — never substitute.
        # Pinned jobs get their offer at creation; a re-match call means
        # decline/expiry, so the job ends here.
        _expire_unmatched(session, job)
        return
    cfg = repo.load_pricing_config(session, job.service_type_id)
    if cfg is None:
        _expire_unmatched(session, job)
        return
    seen = repo.sellers_seen_for_job(session, job.id)
    candidates = repo.eligible_candidates(session, job.service_type_id, seen)
    supply = repo.available_count(session, job.service_type_id)
    demand = repo.active_demand(session, job.service_type_id)
    strategy = STRATEGIES.get(cfg.matching_strategy)
    result = strategy(job.buyer_price, candidates, cfg, supply, demand) if strategy else None
    if result is None:
        _expire_unmatched(session, job)
    else:
        create_offer(session, job, result.seller_id, result.seller_payout)


def expire_open_offers(session: Session, job_id: UUID) -> None:
    # Callers already hold the Job lock, and accept/decline/the offer sweep
    # lock Offer -> Job: waiting here for an offer one of them holds is a
    # deadlock. SKIP LOCKED leaves that offer to its holder, which finds the
    # job no longer PENDING once we commit (accept 409s, decline and the
    # sweep do not re-match).
    for offer in session.scalars(
        select(Offer)
        .where(Offer.job_id == job_id, Offer.status == OfferStatus.OFFERED)
        .with_for_update(skip_locked=True)
    ).all():
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()


def sweep_expired_offers(session: Session) -> None:
    """Lazy sweep: expire timed-out offers and re-match their jobs. Called on reads."""
    # SKIP LOCKED: concurrent sweeps (many read paths, more than one
    # maintenance runner) each claim distinct offers, so one expired offer is
    # never re-matched twice into two live offers. Offer -> Job lock order
    # matches accept/decline.
    stale = session.scalars(
        select(Offer)
        .where(Offer.status == OfferStatus.OFFERED, Offer.expires_at < _now())
        .with_for_update(skip_locked=True)
    ).all()
    for offer in stale:
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()
        job = session.get(Job, offer.job_id, with_for_update=True, populate_existing=True)
        if job is not None and job.status == JobStatus.PENDING:
            match_and_offer(session, job)
