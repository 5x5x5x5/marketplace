"""The seller dashboard, driven through its forms."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Listing, Offer
from marketplace.models import JobStatus, OfferStatus, PaymentStatus
from marketplace.payments.fake import FakeProvider
from tests.conftest import AuthFactory
from tests.page_helpers import csrf_of, page_user, user_id


def post(c: TestClient, path: str, **data: str) -> Any:
    return c.post(path, data={"csrf_token": csrf_of(c, "/seller"), **data}, follow_redirects=False)


def ready_seller(sid: str) -> TestClient:
    """A seller signed up through the page, onboarded, available, with one listing."""
    s = page_user("seller", name="Sam's Rides")
    assert (
        post(s, "/seller/payments/onboard").headers["location"] == "/seller?notice=payments_ready"
    )
    assert post(s, "/seller/availability", service_type_id=sid, on="true").status_code == 303
    r = post(s, "/seller/listings", service_type_id=sid, title="City rides", description="Vetted")
    assert r.headers["location"] == "/seller?notice=listing_created", r.text
    return s


def listing_id_of(seller: TestClient) -> str:
    with SessionLocal() as db:
        listing = db.scalar(select(Listing).where(Listing.seller_id == user_id(seller)))
        assert listing is not None
        return str(listing.id)


def book(client: TestClient, auth: AuthFactory, lid: str, buyer: str = "b1") -> str:
    h = auth("buyer", buyer)
    q = client.post("/v1/quotes", json={"listing_id": lid}, headers=h)
    assert q.status_code == 201, q.text
    j = client.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=h)
    assert j.status_code == 201, j.text
    return str(j.json()["id"])


def only_offer() -> Offer:
    with SessionLocal() as db:
        offer = db.scalar(select(Offer))
        assert offer is not None
        return offer


def test_onboard_link_outage_shows_provider_notice(fake_payments: FakeProvider) -> None:
    s = page_user("seller")
    assert post(s, "/seller/payments/onboard").status_code == 303  # account created
    fake_payments.fail_next_call = True  # now only the onboarding-link call can fail
    r = post(s, "/seller/payments/onboard")
    assert r.status_code == 303 and r.headers["location"] == "/seller?notice=provider"


def test_dashboard_is_for_sellers(client: TestClient) -> None:
    assert page_user("buyer").get("/seller").status_code == 403
    r = TestClient(client.app).get("/seller", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_onboarding_from_the_dashboard(client: TestClient) -> None:
    s = page_user("seller")
    html = s.get("/seller").text
    assert 'action="/seller/payments/onboard"' in html
    r = post(s, "/seller/payments/onboard")
    assert r.headers["location"] == "/seller?notice=payments_ready"
    html = s.get(r.headers["location"]).text
    assert "Payments are set up." in html
    assert 'action="/seller/payments/onboard"' not in html


def test_onboarding_return_url_lands_on_the_dashboard(client: TestClient) -> None:
    r = client.get("/onboarded", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/seller?notice=onboarding_returned"


def test_availability_toggle_is_idempotent(client: TestClient, basic_service: str) -> None:
    s = page_user("seller")
    assert f'value="{basic_service}"' in s.get("/seller").text
    for on, notice in (
        ("true", "available_on"),
        ("true", "available_on"),
        ("false", "available_off"),
        ("false", "available_off"),
    ):
        r = post(s, "/seller/availability", service_type_id=basic_service, on=on)
        assert r.headers["location"] == f"/seller?notice={notice}"


def test_listing_needs_payments_first(client: TestClient, basic_service: str) -> None:
    s = page_user("seller")
    r = post(s, "/seller/listings", service_type_id=basic_service, title="Rides")
    assert r.headers["location"] == "/seller?notice=onboard_first"


def test_create_edit_and_toggle_a_listing(client: TestClient, basic_service: str) -> None:
    s = ready_seller(basic_service)
    lid = listing_id_of(s)
    assert "City rides" in client.get("/").text
    assert f'href="/seller/listings/{lid}/edit"' in s.get("/seller").text

    edit = s.get(f"/seller/listings/{lid}/edit").text
    assert 'value="City rides"' in edit
    r = post(
        s,
        f"/seller/listings/{lid}",
        title="Airport runs",
        description="Any hour",
        photo_urls="https://img.test.local/a.jpg\n\n https://img.test.local/b.jpg ",
    )
    assert r.headers["location"] == "/seller?notice=listing_saved"
    with SessionLocal() as db:
        listing = db.get(Listing, UUID(lid))
        assert listing is not None
        assert listing.title == "Airport runs"
        assert listing.photo_urls == [
            "https://img.test.local/a.jpg",
            "https://img.test.local/b.jpg",
        ]

    r = post(s, f"/seller/listings/{lid}/active", active="false")
    assert r.headers["location"] == "/seller?notice=listing_off"
    assert "Airport runs" not in client.get("/").text
    assert post(s, f"/seller/listings/{lid}/active", active="false").status_code == 303  # repeat
    r = post(s, f"/seller/listings/{lid}/active", active="true")
    assert r.headers["location"] == "/seller?notice=listing_on"
    assert "Airport runs" in client.get("/").text


def test_invalid_listing_rerenders_the_form(client: TestClient, basic_service: str) -> None:
    s = ready_seller(basic_service)
    r = post(
        s, "/seller/listings", service_type_id=basic_service, title="", description="<b>kept</b>"
    )
    assert r.status_code == 400
    assert "&lt;b&gt;kept&lt;/b&gt;" in r.text
    r = post(
        s,
        "/seller/listings",
        service_type_id=basic_service,
        title="Ok",
        photo_urls="http://insecure.test/a.jpg",
    )
    assert r.status_code == 400
    assert "https://" in r.text


def test_other_sellers_listing_is_not_editable(client: TestClient, basic_service: str) -> None:
    s = ready_seller(basic_service)
    lid = listing_id_of(s)
    other = page_user("seller")
    assert other.get(f"/seller/listings/{lid}/edit").status_code == 404
    r = post(other, f"/seller/listings/{lid}", title="Mine now")
    assert r.headers["location"] == "/seller?notice=not_found"
    r = post(other, f"/seller/listings/{lid}/active", active="false")
    assert r.headers["location"] == "/seller?notice=not_found"


def test_accept_then_complete(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    s = ready_seller(basic_service)
    jid = book(client, auth, listing_id_of(s))
    html = s.get("/seller").text
    offer = only_offer()
    assert "$14.00" in html  # the seller's own payout
    assert f'action="/seller/offers/{offer.id}/accept"' in html
    assert f'action="/seller/offers/{offer.id}/decline"' in html

    r = post(s, f"/seller/offers/{offer.id}/accept")
    assert r.headers["location"] == "/seller?notice=accepted"
    html = s.get("/seller").text
    assert f'action="/seller/jobs/{jid}/complete"' in html
    assert f'action="/seller/offers/{offer.id}/accept"' not in html

    r = post(s, f"/seller/jobs/{jid}/complete")
    assert r.headers["location"] == "/seller?notice=completed"
    with SessionLocal() as db:
        job = db.get(Job, UUID(jid))
        assert job is not None and job.status is JobStatus.COMPLETED
    again = post(s, f"/seller/jobs/{jid}/complete")  # double submit is harmless
    assert again.headers["location"] == "/seller?notice=complete_conflict"


def test_accept_with_a_pending_charge(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: Any
) -> None:
    s = ready_seller(basic_service)
    jid = book(client, auth, listing_id_of(s))
    fake_payments.next_charge_status = PaymentStatus.PENDING
    r = post(s, f"/seller/offers/{only_offer().id}/accept")
    assert r.headers["location"] == "/seller?notice=accepted_pending"
    html = s.get("/seller").text
    assert "Awaiting payment" in html
    assert f'action="/seller/jobs/{jid}/complete"' not in html
    assert "cs_fake" not in html  # the buyer's payment secret never reaches the seller


def test_decline_ends_a_pinned_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    s = ready_seller(basic_service)
    jid = book(client, auth, listing_id_of(s))
    r = post(s, f"/seller/offers/{only_offer().id}/decline")
    assert r.headers["location"] == "/seller?notice=declined"
    with SessionLocal() as db:
        job = db.get(Job, UUID(jid))
        assert job is not None and job.status is JobStatus.EXPIRED  # never re-matched


def test_accepting_an_expired_offer_rolls_back(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """accept_offer marks an expired offer EXPIRED and then raises; the JSON
    API discards that by answering 410. The page answers with a 303, so it
    must roll back itself or CommitRoute would commit the partial write."""
    s = ready_seller(basic_service)
    book(client, auth, listing_id_of(s))
    token = csrf_of(s, "/seller")
    offer = only_offer()
    with SessionLocal() as db:
        row = db.get(Offer, offer.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    r = s.post(
        f"/seller/offers/{offer.id}/accept", data={"csrf_token": token}, follow_redirects=False
    )
    assert r.headers["location"] == "/seller?notice=gone"
    assert only_offer().status is OfferStatus.OFFERED  # same as the API's 410


def test_dashboard_routes_are_listed(client: TestClient) -> None:
    html = page_user("seller").get("/seller").text
    for heading in ("Payments", "Availability", "Offers", "Jobs", "Listings"):
        assert re.search(rf"<h2[^>]*>{heading}", html), heading
