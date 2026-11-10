"""Booking: price a quote, then turn it into a job with its first offer.

Two modes share this path. Dispatch quotes a service type and lets matching
pick the seller; browse quotes a listing and pins the job to that listing's
seller (never re-matched to a rival). Quotes are single-use: `create_job`
consumes the quote under a row lock, and a pinned booking that can no longer
be honoured consumes it too, committing before it raises."""

import math
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import repo
from ..catalog import listing_bookable as _listing_bookable
from ..config import Candidate, PricingConfig
from ..entities import Availability, Job, Listing, Quote
from ..errors import ConflictError, GoneError, NotFoundError, OverloadedError, UnprocessableError
from ..matching import passes_floor, required_spread, seller_payout_for
from ..models import JobCreateRequest, QuoteRequest, Side, to_money
from ..pricing import PricingContext, run_pipeline
from ..settings import settings
from .common import now as _now
from .common import require_active as _require_active
from .common import require_verified_email as _require_verified_email
from .offers import create_offer as _create_offer
from .offers import match_and_offer as _match_and_offer

MAX_ACTIVE_QUOTES = 100_000  # ponytail: crude OOM backstop; real limits at the gateway.


def create_quote(session: Session, buyer_id: str, req: QuoteRequest) -> Quote:
    _require_active(session, buyer_id)
    listing: Listing | None = None
    if req.listing_id is not None:
        listing = session.get(Listing, req.listing_id)
        if listing is None or not _listing_bookable(session, listing):
            raise ConflictError("listing unavailable")
        # I1: a listing whose seller has NO Availability row is otherwise
        # fully visible/bookable (`_listing_bookable` never checks
        # Availability) -- every booking against it would consume the quote
        # and 410 at job time with nothing to fall back to. Reject here,
        # before anything is consumed, instead of at job time.
        if (
            session.scalar(
                select(Availability).where(
                    Availability.seller_id == listing.seller_id,
                    Availability.service_type_id == listing.service_type_id,
                )
            )
            is None
        ):
            raise ConflictError("listing unavailable")
        if req.service_type_id is not None and req.service_type_id != listing.service_type_id:
            raise UnprocessableError("listing_id and service_type_id disagree")
        service_type_id = listing.service_type_id
    elif req.service_type_id is not None:
        service_type_id = req.service_type_id
    else:
        raise UnprocessableError("listing_id or service_type_id required")
    cfg = repo.load_pricing_config(session, service_type_id)
    if cfg is None:
        raise NotFoundError("unknown service_type_id")

    # Count live quotes only (index range on expires_at, capped at the limit);
    # expired rows are reaped by housekeeping, not deleted here — concurrent
    # quote writers deleting the same expired rows would block on each other.
    live = select(Quote.id).where(Quote.expires_at > _now()).limit(MAX_ACTIVE_QUOTES).subquery()
    if (session.scalar(select(func.count()).select_from(live)) or 0) >= MAX_ACTIVE_QUOTES:
        raise OverloadedError("quote capacity reached, retry shortly")

    supply = repo.available_count(session, cfg.service.id)
    demand = repo.active_demand(session, cfg.service.id) + 1
    buyer = repo.get_or_create_buyer(session, buyer_id)

    # Cross-reference: mirrors _indicative_buyer_price (catalog's pre-floor
    # "from" price) above — keep both computations in sync.
    ctx = PricingContext(
        side=Side.BUYER,
        buyer_completed_jobs=buyer.completed_jobs,
        live_supply=supply,
        live_demand=demand,
    )
    buyer_price = to_money(
        run_pipeline(
            float(cfg.service.base_buyer_price), cfg.buyer_pipeline, ctx, cfg.adjuster_params
        )
    )

    if listing is not None:
        # Pinned: probe the CHOSEN seller's payout, not the market minimum —
        # the buyer picked this seller, so the floor check must reflect what
        # the platform would actually pay them, not whoever is cheapest.
        pinned_candidate = repo.candidate_for_seller(session, listing.seller_id)
        if pinned_candidate is None:
            raise ConflictError("listing unavailable")
        probe = seller_payout_for(pinned_candidate, cfg, supply, demand)
    else:
        payouts = [
            seller_payout_for(c, cfg, supply, demand)
            for c in repo.eligible_candidates(session, cfg.service.id, set())
        ]
        probe = min(payouts) if payouts else None
    if probe is not None:
        required = required_spread(buyer_price, cfg.margin_floor, cfg.fees)
        if buyer_price - probe < required:
            # Round the corrected price UP to a whole unit so it isn't pinned to
            # exactly probe + required (which would leak the seller's payout).
            # Both the pct floor and the fee grow with the bumped price, so one
            # ceil can undershoot — walk whole units until the invariant holds,
            # bounded by the ceiling.
            ceiling = to_money(cfg.service.base_buyer_price * cfg.margin_floor.ceiling_multiplier)
            target = to_money(math.ceil(probe + required))
            while target <= ceiling and target - probe < required_spread(
                target, cfg.margin_floor, cfg.fees
            ):
                target += 1
            if target > ceiling:
                raise UnprocessableError(
                    "cannot quote: required margin exceeds the configured price ceiling",
                )
            buyer_price = target

    quote = Quote(
        buyer_id=buyer_id,
        service_type_id=cfg.service.id,
        buyer_price=buyer_price,
        expires_at=_now() + timedelta(minutes=settings.quote_ttl_minutes),
        listing_id=req.listing_id,
        pinned_seller_id=(listing.seller_id if listing is not None else None),
    )
    session.add(quote)
    session.flush()
    return quote


def get_quote(session: Session, buyer_id: str, quote_id: UUID) -> Quote:
    """A buyer's own live-or-expired quote. Same 404 for missing and
    not-yours, as `create_job` answers."""
    quote = session.get(Quote, quote_id)
    if quote is None or quote.buyer_id != buyer_id:
        raise NotFoundError("quote not found")
    return quote


def job_for_quote(session: Session, buyer_id: str, quote_id: UUID) -> Job | None:
    """The job this buyer created from `quote_id`, if any. Quotes are deleted
    when consumed, so a repeated booking submit finds its job here."""
    return session.scalar(select(Job).where(Job.quote_id == quote_id, Job.buyer_id == buyer_id))


def create_job(session: Session, buyer_id: str, req: JobCreateRequest) -> Job:
    _require_active(session, buyer_id)
    _require_verified_email(session, buyer_id)
    quote = session.get(Quote, req.quote_id, with_for_update=True)
    # Same 404 whether missing or not-yours — don't confirm someone else's quote.
    if quote is None or quote.buyer_id != buyer_id:
        raise NotFoundError("quote not found")
    if quote.expires_at < _now():
        raise GoneError("quote expired")

    cfg: PricingConfig | None = None
    payout_candidate: Candidate | None = None
    payout: Decimal | None = None
    if quote.pinned_seller_id is not None:
        listing = session.get(Listing, quote.listing_id) if quote.listing_id else None
        has_availability = (
            session.scalar(
                select(Availability).where(
                    Availability.seller_id == quote.pinned_seller_id,
                    Availability.service_type_id == quote.service_type_id,
                )
            )
            is not None
        )
        cfg = repo.load_pricing_config(session, quote.service_type_id)
        payout_candidate = repo.candidate_for_seller(session, quote.pinned_seller_id)
        if (
            listing is None
            or not _listing_bookable(session, listing)
            or not has_availability
            or cfg is None
            or payout_candidate is None
        ):
            # Single-use regardless of booking outcome — the same contract as
            # the unpinned no-seller-found case, which still consumes the
            # quote and simply yields an EXPIRED job. A pinned failure has no
            # rival to fall back to, so it 410s instead of returning a job;
            # commit the consumption here because CommitRoute never persists
            # a 4xx response, and a dead quote must not be retryable.
            session.delete(quote)
            session.commit()
            raise GoneError("listing no longer available")
        # C1: the quote-time probe checked the floor against the pinned
        # seller's payout AS OF THE QUOTE. Tier/pricing can drift inside the
        # quote's TTL window, so recompute the payout at JOB time here — before
        # the Job row is built — and re-check the floor. Compute once and
        # reuse below for `_create_offer` so the offer reflects the same
        # payout the check approved.
        supply = repo.available_count(session, quote.service_type_id)
        demand = repo.active_demand(session, quote.service_type_id)
        payout = seller_payout_for(payout_candidate, cfg, supply, demand)
        if not passes_floor(quote.buyer_price, payout, cfg.margin_floor, cfg.fees):
            # Same mid-commit-consume 410 contract as the guards above — never
            # leak payout/floor numbers in the response.
            session.delete(quote)
            session.commit()
            raise GoneError("listing no longer available")

    job = Job(
        quote_id=quote.id,
        buyer_id=quote.buyer_id,
        service_type_id=quote.service_type_id,
        buyer_price=quote.buyer_price,
    )
    session.add(job)
    session.delete(quote)  # quotes are single-use; deleting also blocks a racing reuse
    session.flush()
    if quote.pinned_seller_id is not None:
        assert cfg is not None and payout_candidate is not None and payout is not None  # guarded
        job.pinned_seller_id = quote.pinned_seller_id
        _create_offer(session, job, quote.pinned_seller_id, payout)
    else:
        _match_and_offer(session, job)  # PENDING with an offer, or EXPIRED if no seller fits
    session.flush()
    return job
