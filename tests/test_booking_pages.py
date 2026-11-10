"""Buyer booking pages, driven through forms the way a browser would."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Job, Payment, Quote
from marketplace.models import JobStatus, PaymentStatus
from marketplace.pages import is_page_post
from marketplace.settings import settings
from tests.conftest import AuthFactory, Header
from tests.page_helpers import csrf_from, csrf_of, page_user
from tests.test_payments import onboard_and_avail
from tests.webhook_helpers import post_event


def listed(client: TestClient, auth: AuthFactory, sid: str, **over: object) -> str:
    """Seller s1 onboarded, available, with one listing; returns its id."""
    onboard_and_avail(client, auth, sid, "s1")
    body: dict[str, object] = {"service_type_id": sid, "title": "City rides"} | over
    r = client.post("/v1/seller/listings", json=body, headers=auth("seller", "s1"))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def quote_via_page(buyer: TestClient, lid: str) -> str:
    token = csrf_of(buyer, f"/listings/{lid}")
    r = buyer.post(f"/listings/{lid}/quote", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/quotes/([0-9a-f-]+)", r.headers["location"])
    assert m, r.headers["location"]
    return m.group(1)


def book_via_page(buyer: TestClient, qid: str) -> Any:
    token = csrf_of(buyer, f"/quotes/{qid}")
    return buyer.post(f"/quotes/{qid}/book", data={"csrf_token": token}, follow_redirects=False)


def booked(buyer: TestClient, lid: str) -> str:
    r = book_via_page(buyer, quote_via_page(buyer, lid))
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/bookings/([0-9a-f-]+)", r.headers["location"])
    assert m, r.headers["location"]
    return m.group(1)


def offer_id(client: TestClient, seller: Header) -> str:
    return str(client.get("/v1/seller/offers", headers=seller).json()[0]["id"])


def test_listing_shows_the_quote_form_to_buyers_only(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    html = page_user("buyer").get(f"/listings/{lid}").text
    assert f'action="/listings/{lid}/quote"' in html
    assert 'name="csrf_token"' in html
    seller_html = page_user("seller").get(f"/listings/{lid}").text
    assert f'action="/listings/{lid}/quote"' not in seller_html
    assert "buyer account" in seller_html


def test_quote_book_and_follow_the_booking_to_completion(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    quote_html = buyer.get(f"/quotes/{qid}").text
    assert "$20.00" in quote_html
    assert "City rides" in quote_html
    assert "Valid until" in quote_html
    assert f'action="/quotes/{qid}/book"' in quote_html

    r = book_via_page(buyer, qid)
    assert r.status_code == 303
    jid = r.headers["location"].rsplit("/", 1)[1]
    html = buyer.get(f"/bookings/{jid}").text
    assert "Waiting for s1" in html
    assert "$20.00" in html
    assert f'action="/bookings/{jid}/cancel"' in html

    seller = auth("seller", "s1")
    assert (
        client.post(
            f"/v1/seller/offers/{offer_id(client, seller)}/accept", headers=seller
        ).status_code
        == 200
    )
    html = buyer.get(f"/bookings/{jid}").text
    assert "Confirmed" in html
    assert f'action="/bookings/{jid}/cancel"' not in html

    assert client.post(f"/v1/seller/jobs/{jid}/complete", headers=seller).status_code == 200
    assert "Completed" in buyer.get(f"/bookings/{jid}").text
    listing_html = buyer.get("/bookings").text
    assert f'href="/bookings/{jid}"' in listing_html
    assert "Completed" in listing_html


def test_second_book_submit_lands_on_the_same_booking(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    token = csrf_of(buyer, f"/quotes/{qid}")
    first = buyer.post(f"/quotes/{qid}/book", data={"csrf_token": token}, follow_redirects=False)
    second = buyer.post(f"/quotes/{qid}/book", data={"csrf_token": token}, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    again = buyer.get(f"/quotes/{qid}", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"] == first.headers["location"]
    with SessionLocal() as s:
        assert len(s.scalars(select(Job)).all()) == 1


def test_expired_quote(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    token = csrf_of(buyer, f"/quotes/{qid}")
    with SessionLocal() as s:
        quote = s.scalar(select(Quote))
        assert quote is not None
        quote.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    page = buyer.get(f"/quotes/{qid}")
    assert page.status_code == 410
    assert f'action="/quotes/{qid}/book"' not in page.text
    assert f'action="/listings/{lid}/quote"' in page.text  # get a fresh price
    r = buyer.post(f"/quotes/{qid}/book", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "notice=quote_expired" in r.headers["location"]


def test_listing_gone_between_quote_and_booking(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    token = csrf_of(buyer, f"/quotes/{qid}")
    assert client.post(f"/v1/admin/listings/{lid}/hide", headers=admin).status_code == 200
    r = buyer.post(f"/quotes/{qid}/book", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "notice=quote_unavailable" in r.headers["location"]
    with SessionLocal() as s:  # the dead quote stays consumed (committed before the 410)
        assert s.scalar(select(Quote)) is None
        assert s.scalar(select(Job)) is None
    assert "This listing can&#39;t be booked right now." in buyer.get(r.headers["location"]).text


def test_cancel_pending_booking_and_repeat(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    jid = booked(buyer, lid)
    token = csrf_of(buyer, f"/bookings/{jid}")
    r = buyer.post(f"/bookings/{jid}/cancel", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/bookings/{jid}?notice=cancelled"
    assert "Cancelled" in buyer.get(r.headers["location"]).text
    again = buyer.post(
        f"/bookings/{jid}/cancel", data={"csrf_token": token}, follow_redirects=False
    )
    assert again.headers["location"] == f"/bookings/{jid}?notice=cancel_conflict"


def test_booking_pages_are_owner_only(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    other = page_user("buyer")
    assert other.get(f"/quotes/{qid}").status_code == 404
    jid = booked(buyer, lid)
    assert other.get(f"/bookings/{jid}").status_code == 404
    assert other.get(f"/bookings/{jid}/status").status_code == 404
    token = csrf_of(other)
    r = other.post(f"/bookings/{jid}/cancel", data={"csrf_token": token}, follow_redirects=False)
    assert "notice=not_found" in r.headers["location"]
    with SessionLocal() as s:
        job = s.scalar(select(Job))
        assert job is not None and job.status is JobStatus.PENDING
    assert page_user("seller").get(f"/bookings/{jid}").status_code == 403
    anonymous = TestClient(client.app)
    r = anonymous.get(f"/bookings/{jid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_status_fragment_asks_for_a_refresh_when_status_moves(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    jid = booked(buyer, lid)
    html = buyer.get(f"/bookings/{jid}").text
    assert f'hx-get="/bookings/{jid}/status?shown=pending"' in html
    r = buyer.get(f"/bookings/{jid}/status?shown=pending")
    assert r.status_code == 200 and "HX-Refresh" not in r.headers
    seller = auth("seller", "s1")
    client.post(f"/v1/seller/offers/{offer_id(client, seller)}/accept", headers=seller)
    r = buyer.get(f"/bookings/{jid}/status?shown=pending")
    assert r.headers.get("HX-Refresh") == "true"
    assert "hx-get" not in buyer.get(f"/bookings/{jid}").text  # nothing left to poll for


def test_gate_on_page_booking_explains_verification(
    client: TestClient, basic_service: str, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    qid = quote_via_page(buyer, lid)
    monkeypatch.setattr(settings, "require_verified_email", True)
    r = book_via_page(buyer, qid)
    assert r.headers["location"] == f"/quotes/{qid}?notice=unverified"
    html = buyer.get(r.headers["location"]).text
    assert "Verify your email address first" in html
    assert 'action="/verify/resend"' in html


def test_booking_routes_are_page_posts() -> None:
    assert is_page_post("/bookings/00000000-0000-0000-0000-000000000000/cancel")
    assert is_page_post("/quotes/00000000-0000-0000-0000-000000000000/book")


def test_bookings_list_needs_a_buyer(client: TestClient) -> None:
    buyer = page_user("buyer")
    assert "No bookings yet" in buyer.get("/bookings").text
    assert csrf_from(buyer.get("/bookings").text)


# ---------- the payment step ----------


def _awaiting_payment(
    client: TestClient, auth: AuthFactory, sid: str, fake_payments: Any
) -> tuple[TestClient, str, str]:
    """A booking the seller accepted whose charge is still pending.
    Returns (buyer client, job id, provider payment id)."""
    lid = listed(client, auth, sid)
    buyer = page_user("buyer")
    jid = booked(buyer, lid)
    fake_payments.next_charge_status = PaymentStatus.PENDING
    seller = auth("seller", "s1")
    r = client.post(f"/v1/seller/offers/{offer_id(client, seller)}/accept", headers=seller)
    assert r.json()["status"] == "awaiting_payment", r.text
    with SessionLocal() as s:
        payment = s.scalar(select(Payment))
        assert payment is not None and payment.client_secret
        return buyer, jid, payment.provider_payment_id


def test_fake_pending_payment_shows_the_pending_state_without_stripe(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: Any
) -> None:
    buyer, jid, pay_id = _awaiting_payment(client, auth, basic_service, fake_payments)
    html = buyer.get(f"/bookings/{jid}").text
    assert "Accepted, waiting for payment" in html
    assert "being confirmed by the payment provider" in html
    assert "js.stripe.com" not in html
    assert "payment.js" not in html
    assert "cs_fake" not in html  # nothing to mount, so the secret stays server-side
    assert f'action="/bookings/{jid}/cancel"' in html
    post_event(client, event_id="evt-pay-1", kind="payment_succeeded", object_id=pay_id)
    assert (
        buyer.get(f"/bookings/{jid}/status?shown=awaiting_payment").headers.get("HX-Refresh")
        == "true"
    )
    html = buyer.get(f"/bookings/{jid}").text
    assert "Confirmed" in html
    assert "being confirmed" not in html


def test_stripe_payment_element_reaches_only_the_owner_while_pending(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    fake_payments: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Script the singleton as a provider with a browser-side step (Stripe).
    monkeypatch.setattr(fake_payments, "browser_key", "pk_test_page")
    buyer, jid, pay_id = _awaiting_payment(client, auth, basic_service, fake_payments)
    with SessionLocal() as s:
        payment = s.scalar(select(Payment))
        assert payment is not None
        secret = payment.client_secret
    assert secret
    html = buyer.get(f"/bookings/{jid}").text
    assert '<script src="https://js.stripe.com/endive/stripe.js"></script>' in html
    assert '<script src="/static/payment.js" defer></script>' in html
    assert 'data-publishable-key="pk_test_page"' in html
    assert f'data-client-secret="{secret}"' in html
    assert f'data-return-url="{settings.base_url}/bookings/{jid}"' in html

    other = page_user("buyer")
    assert secret not in other.get(f"/bookings/{jid}").text
    assert secret not in buyer.get(f"/bookings/{jid}/status").text  # fragment never carries it
    assert secret not in buyer.get("/bookings").text

    post_event(client, event_id="evt-pay-2", kind="payment_succeeded", object_id=pay_id)
    html = buyer.get(f"/bookings/{jid}").text
    assert "js.stripe.com" not in html
    assert secret not in html
    assert "pk_test_page" not in html


def test_no_stripe_script_before_acceptance_even_with_a_browser_key(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    monkeypatch: pytest.MonkeyPatch,
    fake_payments: Any,
) -> None:
    monkeypatch.setattr(fake_payments, "browser_key", "pk_test_page")
    lid = listed(client, auth, basic_service)
    buyer = page_user("buyer")
    jid = booked(buyer, lid)
    html = buyer.get(f"/bookings/{jid}").text
    assert "js.stripe.com" not in html
    assert "pk_test_page" not in html


def test_payment_script_is_served() -> None:
    from marketplace import api

    r = TestClient(api.app).get("/static/payment.js")
    assert r.status_code == 200
    assert "confirmPayment" in r.text
    assert "replaceState" in r.text  # strips Stripe's return parameters
