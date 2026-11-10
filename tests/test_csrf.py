"""Session-bound CSRF tokens on authenticated page forms."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Listing, Offer, Quote
from marketplace.models import JobStatus, OfferStatus
from marketplace.pages.csrf import CSRF_EXEMPT_POSTS, token_for
from tests.conftest import AuthFactory
from tests.page_helpers import csrf_of, page_user
from tests.test_booking_pages import booked, listed, quote_via_page


def test_token_is_domain_separated_from_the_stored_session_hash() -> None:
    raw = "some-raw-session-token"
    assert token_for(raw) != hashlib.sha256(raw.encode()).hexdigest()
    assert token_for(raw) != token_for(raw + "x")
    assert token_for(raw) == token_for(raw)  # deterministic: no server state


def test_rendered_token_matches_the_cookie_session() -> None:
    c = page_user("buyer")
    raw = c.cookies["session_token"]
    assert csrf_of(c) == token_for(raw)


def test_token_changes_with_each_login() -> None:
    a, b = page_user("buyer"), page_user("buyer")
    assert csrf_of(a) != csrf_of(b)


def test_logout_without_token_is_refused_and_the_session_survives() -> None:
    c = page_user("buyer")
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 403
    assert c.get("/v1/auth/me").status_code == 200


def test_logout_with_wrong_token_is_refused() -> None:
    c = page_user("buyer")
    other = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": csrf_of(other)}, follow_redirects=False)
    assert r.status_code == 403
    assert c.get("/v1/auth/me").status_code == 200


def test_logout_with_token_works(client: TestClient) -> None:
    c = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": csrf_of(c)}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/v1/auth/me").status_code == 401


def test_non_ascii_token_fails_closed_not_500() -> None:
    c = page_user("buyer")
    r = c.post("/logout", data={"csrf_token": "é" * 64}, follow_redirects=False)
    assert r.status_code == 403


def test_exempt_list_is_exactly_the_unauthenticated_forms() -> None:
    assert {
        "/signup",
        "/login",
        "/verify",
        "/password-reset/request",
        "/password-reset/confirm",
    } == CSRF_EXEMPT_POSTS


# ---------- a forged cross-site POST with a valid cookie is refused ----------
#
# The attacker's page can make the victim's browser send the session cookie
# (that is the premise of CSRF) but cannot read the victim's pages, so it can
# send no token or a token of its own session. Both must fail closed with no
# state change, for every form that moves money or job state.

_FORGERIES = [pytest.param({}, id="no-token"), pytest.param(None, id="attackers-own-token")]


def _forge(victim: TestClient, path: str, data: dict[str, str] | None) -> int:
    if data is None:  # a valid token, but for the attacker's own session
        data = {"csrf_token": csrf_of(page_user("buyer"))}
    return victim.post(path, data=data, follow_redirects=False).status_code


def _count(model: type[Quote] | type[Job]) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_quote_request_is_refused(
    client: TestClient, basic_service: str, auth: AuthFactory, forged: dict[str, str] | None
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    assert _forge(buyer, f"/listings/{lid}/quote", forged) == 403
    assert _count(Quote) == 0


@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_booking_is_refused(
    client: TestClient, basic_service: str, auth: AuthFactory, forged: dict[str, str] | None
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    assert _forge(buyer, f"/quotes/{qid}/book", forged) == 403
    assert _count(Job) == 0 and _count(Quote) == 1


@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_cancel_is_refused(
    client: TestClient, basic_service: str, auth: AuthFactory, forged: dict[str, str] | None
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    jid = booked(buyer, lid)
    assert _forge(buyer, f"/bookings/{jid}/cancel", forged) == 403
    with SessionLocal() as s:
        job = s.scalar(select(Job))
        assert job is not None and job.status is JobStatus.PENDING


def _seller_with_offer(client: TestClient, auth: AuthFactory, sid: str) -> tuple[TestClient, str]:
    """A page-session seller holding one open offer from an API buyer."""
    seller = page_user("seller")
    token = csrf_of(seller, "/seller")
    for path, data in (
        ("/seller/payments/onboard", {}),
        ("/seller/availability", {"service_type_id": sid, "on": "true"}),
        ("/seller/listings", {"service_type_id": sid, "title": "Rides"}),
    ):
        r = seller.post(path, data={"csrf_token": token, **data}, follow_redirects=False)
        assert r.status_code == 303, r.text
    with SessionLocal() as s:
        listing = s.scalar(select(Listing))
        assert listing is not None
        lid = str(listing.id)
    h = auth("buyer", "b1")
    q = client.post("/v1/quotes", json={"listing_id": lid}, headers=h).json()
    assert client.post("/v1/jobs", json={"quote_id": q["id"]}, headers=h).status_code == 201
    with SessionLocal() as s:
        offer = s.scalar(select(Offer))
        assert offer is not None
        return seller, str(offer.id)


@pytest.mark.parametrize("action", ["accept", "decline"])
@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_offer_response_is_refused(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    forged: dict[str, str] | None,
    action: str,
) -> None:
    seller, oid = _seller_with_offer(client, auth, basic_service)
    assert _forge(seller, f"/seller/offers/{oid}/{action}", forged) == 403
    with SessionLocal() as s:
        offer = s.scalar(select(Offer))
        assert offer is not None and offer.status is OfferStatus.OFFERED


@pytest.mark.parametrize("forged", _FORGERIES)
def test_forged_completion_is_refused(
    client: TestClient, basic_service: str, auth: AuthFactory, forged: dict[str, str] | None
) -> None:
    seller, oid = _seller_with_offer(client, auth, basic_service)
    r = seller.post(
        f"/seller/offers/{oid}/accept",
        data={"csrf_token": csrf_of(seller, "/seller")},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/seller?notice=accepted"
    with SessionLocal() as s:
        job = s.scalar(select(Job))
        assert job is not None
        jid = job.id
    assert _forge(seller, f"/seller/jobs/{jid}/complete", forged) == 403
    with SessionLocal() as s:
        job = s.get(Job, jid)
        assert job is not None and job.status is JobStatus.ACCEPTED
