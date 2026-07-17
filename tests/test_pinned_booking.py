"""Chosen-seller (pinned) booking: quotes/jobs anchored to a specific listing's
seller bypass matching entirely, and use human-paced TTLs."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Offer, Payment, Quote, SellerProfile
from marketplace.settings import settings
from tests.conftest import AuthFactory, Header


def _seller_onboarded(client: TestClient, auth: AuthFactory, name: str, tier: str = "") -> Header:
    if tier:
        r = client.put(
            f"/v1/admin/sellers/{name}", json={"tier": tier}, headers=auth("admin", "ops")
        )
        assert r.status_code == 200
    headers = auth("seller", name)
    r = client.post("/v1/seller/payments/onboard", headers=headers)
    assert r.status_code == 200
    return headers


def _avail(client: TestClient, auth: AuthFactory, sid: str, name: str) -> None:
    r = client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", name)
    )
    assert r.status_code == 200


def _listing(client: TestClient, headers: Header, sid: str) -> dict[str, object]:
    r = client.post(
        "/v1/seller/listings", json={"service_type_id": sid, "title": "Deep clean"}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def _offered_to(client: TestClient, auth: AuthFactory, names: list[str]) -> str:
    for n in names:
        if client.get("/v1/seller/offers", headers=auth("seller", n)).json():
            return n
    return ""


def _tiered_pipeline(client: TestClient, admin: Header, sid: str, tiers: dict[str, float]) -> None:
    client.put(
        "/v1/admin/config/adjuster_params/seller_tier_multiplier",
        json={"tiers": tiers},
        headers=admin,
    )
    client.put(
        f"/v1/admin/config/pipelines/{sid}",
        json={"buyer": [], "seller": ["seller_tier_multiplier"]},
        headers=admin,
    )


def test_pinned_offer_lands_on_chosen_seller(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """cheapest_payout would pick the cheap rival; a pinned quote must bypass
    matching entirely and land on the buyer's chosen (expensive) seller."""
    _tiered_pipeline(client, admin, basic_service, {"cheap": 0.7, "chosen": 1.5})
    cheap = _seller_onboarded(client, auth, "cheap_rival", "cheap")
    _avail(client, auth, basic_service, "cheap_rival")
    chosen = _seller_onboarded(client, auth, "chosen_seller", "chosen")
    _avail(client, auth, basic_service, "chosen_seller")
    listing = _listing(client, chosen, basic_service)

    buyer = auth("buyer", "pin_buyer")
    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()
    job = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert job.status_code == 201, job.text

    assert _offered_to(client, auth, ["cheap_rival", "chosen_seller"]) == "chosen_seller"
    assert not client.get("/v1/seller/offers", headers=cheap).json()


def test_pinned_floor_probes_pinned_payout(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """The floor probe must use the PINNED seller's payout, not the market
    minimum: a floor that the cheap rival clears easily but the pinned
    (expensive) seller does not must still bump the pinned quote's price."""
    _tiered_pipeline(client, admin, basic_service, {"cheap": 0.5, "expensive": 1.4})
    _seller_onboarded(client, auth, "floor_cheap", "cheap")
    _avail(client, auth, basic_service, "floor_cheap")
    expensive = _seller_onboarded(client, auth, "floor_expensive", "expensive")
    _avail(client, auth, basic_service, "floor_expensive")
    listing = _listing(client, expensive, basic_service)

    client.put("/v1/admin/config/margin_floor", json={"absolute": 5}, headers=admin)

    buyer = auth("buyer", "floor_buyer")
    unpinned = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=buyer
    ).json()
    pinned = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()

    # cheap rival (payout 7.00) clears floor 5 + fee(20) = 5.88 with room to
    # spare -> unpinned quote stays at base price 20.00.
    assert unpinned["buyer_price"] == "20.00"
    # expensive pinned seller (payout 19.60) needs a bump: first ceil (26)
    # clears required = 5 + fee(26) = 6.05 (spread 6.40) -- same derivation
    # style as test_margin_floor.py's test_floor_bumps_quote_up.
    assert pinned["buyer_price"] == "26.00"
    assert pinned["buyer_price"] != unpinned["buyer_price"]


def test_quote_validation_matrix(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = _seller_onboarded(client, auth, "matrix_seller")
    _avail(client, auth, basic_service, "matrix_seller")
    listing = _listing(client, seller, basic_service)
    other_sid = "matrix_other"
    r = client.put(
        f"/v1/admin/config/service_types/{other_sid}",
        json={"base_buyer_price": 30, "base_seller_payout": 20},
        headers=admin,
    )
    assert r.status_code == 200

    buyer = auth("buyer", "matrix_buyer")

    # listing_id only -> service_type_id filled from the listing, pinned_seller_id set.
    r = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["service_type_id"] == basic_service
    assert body["pinned_seller_id"] == "matrix_seller"
    assert body["listing_id"] == listing["id"]

    # both agreeing -> OK.
    r = client.post(
        "/v1/quotes",
        json={"listing_id": listing["id"], "service_type_id": basic_service},
        headers=buyer,
    )
    assert r.status_code == 201, r.text

    # both disagreeing -> 422.
    r = client.post(
        "/v1/quotes",
        json={"listing_id": listing["id"], "service_type_id": other_sid},
        headers=buyer,
    )
    assert r.status_code == 422

    # neither -> 422.
    r = client.post("/v1/quotes", json={}, headers=buyer)
    assert r.status_code == 422


def test_quote_409_when_unavailable(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    buyer = auth("buyer", "unavail_buyer")

    # Reason 1: listing deactivated by its own seller.
    s1 = _seller_onboarded(client, auth, "un_inactive")
    l1 = _listing(client, s1, basic_service)
    r = client.put(f"/v1/seller/listings/{l1['id']}", json={"active": False}, headers=s1)
    assert r.status_code == 200
    r = client.post("/v1/quotes", json={"listing_id": l1["id"]}, headers=buyer)
    assert r.status_code == 409
    assert r.json()["detail"] == "listing unavailable"

    # Reason 2: hidden by admin takedown.
    s2 = _seller_onboarded(client, auth, "un_hidden")
    l2 = _listing(client, s2, basic_service)
    assert client.post(f"/v1/admin/listings/{l2['id']}/hide", headers=admin).status_code == 200
    r = client.post("/v1/quotes", json={"listing_id": l2["id"]}, headers=buyer)
    assert r.status_code == 409
    assert r.json()["detail"] == "listing unavailable"

    # Reason 3: seller suspended.
    s3 = _seller_onboarded(client, auth, "un_suspended")
    l3 = _listing(client, s3, basic_service)
    assert (
        client.post(
            "/v1/admin/users/un_suspended/suspend", json={"reason": "test"}, headers=admin
        ).status_code
        == 200
    )
    r = client.post("/v1/quotes", json={"listing_id": l3["id"]}, headers=buyer)
    assert r.status_code == 409
    assert r.json()["detail"] == "listing unavailable"

    # Reason 4: payments_ready flipped false after listing creation (white-box
    # -- payments_ready otherwise only flips via the account webhook).
    s4 = _seller_onboarded(client, auth, "un_notready")
    l4 = _listing(client, s4, basic_service)
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "un_notready")
        assert prof is not None
        prof.payments_ready = False
        s.commit()
    r = client.post("/v1/quotes", json={"listing_id": l4["id"]}, headers=buyer)
    assert r.status_code == 409
    assert r.json()["detail"] == "listing unavailable"

    # Reason 5 (I1): seller has no Availability row for the listing's service
    # type. Fully visible/bookable by `_listing_bookable` (which never checks
    # Availability), so without this guard the quote would succeed and every
    # subsequent POST /v1/jobs would 410 -- a guaranteed dead end. Deliberately
    # skips `_avail` for this seller.
    s5 = _seller_onboarded(client, auth, "un_noavail")
    l5 = _listing(client, s5, basic_service)
    r = client.post("/v1/quotes", json={"listing_id": l5["id"]}, headers=buyer)
    assert r.status_code == 409
    assert r.json()["detail"] == "listing unavailable"
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Quote)) == 0


def test_job_410_when_listing_dies_between_quote_and_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _seller_onboarded(client, auth, "dies_seller")
    _avail(client, auth, basic_service, "dies_seller")
    listing = _listing(client, seller, basic_service)
    buyer = auth("buyer", "dies_buyer")

    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()

    r = client.put(f"/v1/seller/listings/{listing['id']}", json={"active": False}, headers=seller)
    assert r.status_code == 200

    r = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert r.status_code == 410

    # The quote is single-use regardless of the booking outcome -- a retry
    # finds nothing to consume.
    r2 = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert r2.status_code == 404

    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Offer)) == 0
        assert s.scalar(select(func.count()).select_from(Payment)) == 0


def test_job_410_when_tier_promotion_breaks_floor_at_job_time(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """C1: the pinned quote-time probe checked the seller's tier AT QUOTE
    TIME. If an admin promotes that seller's tier inside the quote's 15-min
    TTL window, the job-time payout can violate the margin floor even though
    the quote itself was never bumped -- `create_job`'s pinned branch must
    recompute the payout and re-check the floor before booking, not just
    reuse whatever `_listing_bookable`/availability guards already cover."""
    _tiered_pipeline(client, admin, basic_service, {"cheap": 0.5, "chosen": 1.4})
    seller = _seller_onboarded(client, auth, "promo_seller", "cheap")
    _avail(client, auth, basic_service, "promo_seller")
    listing = _listing(client, seller, basic_service)
    client.put("/v1/admin/config/margin_floor", json={"absolute": 5}, headers=admin)

    buyer = auth("buyer", "promo_buyer")
    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()
    # cheap tier (payout 7.00) clears required 5.88 with room to spare -> no
    # quote-time bump.
    assert quote["buyer_price"] == "20.00"

    # Promote the pinned seller to a tier whose payout (14 * 1.4 = 19.60)
    # leaves only a 0.40 spread against buyer_price 20.00 -- required is 5.88
    # (floor 5 + fee(20) 0.88). The quote is untouched; only the seller's
    # tier moved.
    r = client.put("/v1/admin/sellers/promo_seller", json={"tier": "chosen"}, headers=admin)
    assert r.status_code == 200

    r = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert r.status_code == 410
    # Generic detail -- never leak payout/floor numbers in the response.
    assert r.json()["detail"] == "listing no longer available"

    # Single-use regardless of outcome: a retry finds nothing to consume.
    r2 = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert r2.status_code == 404

    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Quote)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        assert s.scalar(select(func.count()).select_from(Offer)) == 0
        assert s.scalar(select(func.count()).select_from(Payment)) == 0


def test_pinned_offer_books_job_time_payout(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Happy path counterpart to the C1 fix: a tier promotion between quote
    and job that still clears the floor must book at the JOB-TIME payout
    (computed once, right before the Job row is built), not whatever the
    quote-time probe implied."""
    _tiered_pipeline(client, admin, basic_service, {"low": 0.8, "mid": 1.0})
    seller = _seller_onboarded(client, auth, "jobtime_seller", "low")
    _avail(client, auth, basic_service, "jobtime_seller")
    listing = _listing(client, seller, basic_service)

    buyer = auth("buyer", "jobtime_buyer")
    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()

    # Promote after the quote -- low tier payout would have been 14*0.8 =
    # 11.20; mid tier is 14*1.0 = 14.00. Both clear the (unconfigured, hence
    # zero-absolute/pct) floor easily, so the job still books.
    r = client.put("/v1/admin/sellers/jobtime_seller", json={"tier": "mid"}, headers=admin)
    assert r.status_code == 200

    job = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer)
    assert job.status_code == 201, job.text

    offers = client.get("/v1/seller/offers", headers=auth("seller", "jobtime_seller")).json()
    assert len(offers) == 1
    assert offers[0]["seller_payout"] == "14.00"


def test_pinned_decline_expires_never_rematches(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    pinned = _seller_onboarded(client, auth, "decline_pinned")
    _avail(client, auth, basic_service, "decline_pinned")
    listing = _listing(client, pinned, basic_service)

    _seller_onboarded(client, auth, "decline_rival")
    _avail(client, auth, basic_service, "decline_rival")

    buyer = auth("buyer", "decline_buyer")
    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()
    job = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer).json()
    assert job["status"] == "pending"

    offers = client.get("/v1/seller/offers", headers=pinned).json()
    assert len(offers) == 1
    offer_id = offers[0]["id"]

    r = client.post(f"/v1/seller/offers/{offer_id}/decline", headers=pinned)
    assert r.status_code == 200

    body = client.get(f"/v1/jobs/{job['id']}", headers=buyer).json()
    assert body["status"] == "expired"
    assert not client.get("/v1/seller/offers", headers=auth("seller", "decline_rival")).json()


def test_pinned_offer_expiry_expires_job(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    pinned = _seller_onboarded(client, auth, "expiry_pinned")
    _avail(client, auth, basic_service, "expiry_pinned")
    listing = _listing(client, pinned, basic_service)

    _seller_onboarded(client, auth, "expiry_rival")
    _avail(client, auth, basic_service, "expiry_rival")

    buyer = auth("buyer", "expiry_buyer")
    quote = client.post("/v1/quotes", json={"listing_id": listing["id"]}, headers=buyer).json()
    job = client.post("/v1/jobs", json={"quote_id": quote["id"]}, headers=buyer).json()

    # White-box the offer's expiry into the past (house idiom, test_lifecycle.py).
    with SessionLocal() as s:
        offer = (
            s.query(Offer).filter(Offer.job_id == UUID(job["id"]), Offer.status == "offered").one()
        )
        offer.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()

    assert client.post("/v1/admin/jobs/sweep", headers=admin).status_code == 200

    body = client.get(f"/v1/jobs/{job['id']}", headers=buyer).json()
    assert body["status"] == "expired"
    assert not client.get("/v1/seller/offers", headers=auth("seller", "expiry_rival")).json()


def test_dispatch_unchanged(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """No listing_id -> the existing strategy path (cheapest_payout) picks the
    cheapest candidate exactly as before pinning existed."""
    _tiered_pipeline(client, admin, basic_service, {"premium": 1.5, "standard": 1.0, "new": 0.7})
    for name, tier in (
        ("premium_pat", "premium"),
        ("standard_sam", "standard"),
        ("new_nat", "new"),
    ):
        _seller_onboarded(client, auth, name, tier)
        _avail(client, auth, basic_service, name)

    buyer = auth("buyer", "dispatch_buyer")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    r = client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer)
    assert r.status_code == 201, r.text

    assert _offered_to(client, auth, ["premium_pat", "standard_sam", "new_nat"]) == "new_nat"


def test_ttl_defaults() -> None:
    """Guards the fork divergence from upstream TTL defaults."""
    assert settings.quote_ttl_minutes == 15
    assert settings.offer_ttl_minutes == 120
