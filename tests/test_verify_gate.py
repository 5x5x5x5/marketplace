"""The opt-in email-verification gate (enforced in the services, so the JSON
API and the pages behave the same), plus the verify and reset pages."""

import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import EmailToken, Offer, Quote, User
from marketplace.mail import RecordingEmailSender
from marketplace.models import OfferStatus
from marketplace.settings import settings
from tests.conftest import TEST_PASSWORD, AuthFactory
from tests.page_helpers import csrf_of, page_user, user_id
from tests.test_payments import onboard_and_avail

UNVERIFIED = "verify your email address first"


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "require_verified_email", True)


def _verify(user_id: str) -> None:
    with SessionLocal() as s:
        user = s.get(User, user_id)
        assert user is not None
        user.email_verified = True
        s.commit()


def _offer_for(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    q = client.post("/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", "b1"))
    j = client.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=auth("buyer", "b1"))
    assert j.status_code == 201, j.text
    offers = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()
    return str(offers[0]["id"])


def test_gate_is_off_by_default(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    assert settings.require_verified_email is False
    _offer_for(client, auth, basic_service)  # unverified buyer books fine


@pytest.mark.usefixtures("gate_on")
def test_gate_blocks_booking_and_keeps_the_quote(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    buyer = auth("buyer", "b1")
    q = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert q.status_code == 201  # quoting moves nothing and stays open
    r = client.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer)
    assert r.status_code == 403
    assert r.json()["detail"] == UNVERIFIED
    with SessionLocal() as s:
        assert s.scalar(select(Quote)) is not None  # not consumed
    _verify("b1")
    r = client.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer)
    assert r.status_code == 201, r.text


def test_gate_blocks_accept_and_leaves_the_offer_open(
    client: TestClient, basic_service: str, auth: AuthFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer_id = _offer_for(client, auth, basic_service)
    monkeypatch.setattr(settings, "require_verified_email", True)
    seller = auth("seller", "s1")
    r = client.post(f"/v1/seller/offers/{offer_id}/accept", headers=seller)
    assert r.status_code == 403
    assert r.json()["detail"] == UNVERIFIED
    with SessionLocal() as s:
        offer = s.scalar(select(Offer))
        assert offer is not None and offer.status is OfferStatus.OFFERED
    _verify("s1")
    assert client.post(f"/v1/seller/offers/{offer_id}/accept", headers=seller).status_code == 200


@pytest.mark.usefixtures("gate_on")
def test_gate_blocks_listing_creation(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    body = {"service_type_id": basic_service, "title": "Rides"}
    seller = auth("seller", "s1")
    r = client.post("/v1/seller/listings", json=body, headers=seller)
    assert r.status_code == 403
    assert r.json()["detail"] == UNVERIFIED
    _verify("s1")
    assert client.post("/v1/seller/listings", json=body, headers=seller).status_code == 201


def test_buyer_job_view_names_the_pinned_seller(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    seller = auth("seller", "s1")
    lid = client.post(
        "/v1/seller/listings", json={"service_type_id": basic_service, "title": "R"}, headers=seller
    ).json()["id"]
    buyer = auth("buyer", "b1")
    q = client.post("/v1/quotes", json={"listing_id": lid}, headers=buyer).json()
    job = client.post("/v1/jobs", json={"quote_id": q["id"]}, headers=buyer).json()
    assert job["pinned_seller_id"] == "s1"
    assert "seller_payout" not in job


def test_seller_profile_view_carries_own_payments_ready(
    client: TestClient, auth: AuthFactory
) -> None:
    seller = auth("seller", "s1")
    assert client.get("/v1/seller/profile", headers=seller).json()["payments_ready"] is False
    client.post("/v1/seller/payments/onboard", headers=seller)
    assert client.get("/v1/seller/profile", headers=seller).json()["payments_ready"] is True


# ---------- verify and password-reset pages ----------

_LINK = re.compile(r"/(verify|password-reset/confirm)\?token=([A-Za-z0-9_\-]+)")


def _token(outbox: RecordingEmailSender, kind: str = "verify") -> str:
    for _, _, body in reversed(outbox.sent):
        m = _LINK.search(body)
        if m and m.group(1) == kind:
            return m.group(2)
    raise AssertionError(f"no {kind} link mailed")


def _tokens() -> list[EmailToken]:
    with SessionLocal() as s:
        return list(s.scalars(select(EmailToken)).all())


def test_verify_link_get_does_not_consume_the_token(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    buyer = page_user("buyer")
    token = _token(mail_outbox)
    r = client.get(f"/verify?token={token}")
    assert r.status_code == 200
    assert f'value="{token}"' in r.text and 'action="/verify"' in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "same-origin"
    assert all(t.used_at is None for t in _tokens())  # a mail scanner's GET changes nothing
    r = client.post("/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/verify/done"
    assert "verified" in client.get("/verify/done").text
    with SessionLocal() as s:
        user = s.get(User, user_id(buyer))
        assert user is not None and user.email_verified
    again = client.post("/verify", data={"token": token}, follow_redirects=False)
    assert again.status_code == 400  # single use


def test_bad_verify_token_is_a_generic_error(client: TestClient) -> None:
    r = client.post("/verify", data={"token": "not-a-token"}, follow_redirects=False)
    assert r.status_code == 400
    assert "invalid or has expired" in r.text


def test_resend_verification(client: TestClient, mail_outbox: RecordingEmailSender) -> None:
    buyer = page_user("buyer")
    assert len(mail_outbox.sent) == 1
    assert buyer.post("/verify/resend", follow_redirects=False).status_code == 403  # CSRF
    r = buyer.post("/verify/resend", data={"csrf_token": csrf_of(buyer)}, follow_redirects=False)
    assert r.headers["location"] == "/bookings?notice=verification_sent"
    assert len(mail_outbox.sent) == 2
    buyer.post("/verify", data={"token": _token(mail_outbox)})
    r = buyer.post("/verify/resend", data={"csrf_token": csrf_of(buyer)}, follow_redirects=False)
    assert r.headers["location"] == "/bookings?notice=already_verified"
    assert len(mail_outbox.sent) == 2


def test_reset_request_answers_the_same_for_unknown_addresses(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    page_user("buyer", email="known@example.test")
    mail_outbox.sent.clear()
    assert 'action="/password-reset/request"' in client.get("/password-reset").text
    known = client.post(
        "/password-reset/request",
        data={"email": "known@example.test", "role": "buyer"},
        follow_redirects=False,
    )
    unknown = client.post(
        "/password-reset/request",
        data={"email": "nobody@example.test", "role": "buyer"},
        follow_redirects=False,
    )
    malformed = client.post(
        "/password-reset/request", data={"email": "nope", "role": "x"}, follow_redirects=False
    )
    assert known.status_code == unknown.status_code == malformed.status_code == 303
    assert known.headers["location"] == unknown.headers["location"] == "/password-reset/sent"
    assert malformed.headers["location"] == "/password-reset/sent"
    assert len(mail_outbox.sent) == 1  # only the real account got mail
    assert "If an account" in client.get("/password-reset/sent").text


def test_reset_confirm_changes_the_password_and_ends_sessions(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    buyer = page_user("buyer", email="resetme@example.test")
    old_session = buyer.cookies["session_token"]
    client.post("/password-reset/request", data={"email": "resetme@example.test", "role": "buyer"})
    token = _token(mail_outbox, "password-reset/confirm")
    form = client.get(f"/password-reset/confirm?token={token}")
    assert f'value="{token}"' in form.text
    assert form.headers["cache-control"] == "no-store"
    short = client.post(
        "/password-reset/confirm",
        data={"token": token, "new_password": "short"},
        follow_redirects=False,
    )
    assert short.status_code == 400
    assert f'value="{token}"' in short.text  # the form keeps the token for a retry
    r = buyer.post(
        "/password-reset/confirm",
        data={"token": token, "new_password": "a-brand-new-password"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?notice=password_reset"
    assert "Max-Age=0" in r.headers["set-cookie"] or 'session_token=""' in r.headers["set-cookie"]
    assert "password was changed" in client.get(r.headers["location"]).text
    revoked = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {old_session}"})
    assert revoked.status_code == 401  # every session of the account was revoked
    login = client.post(
        "/login",
        data={"email": "resetme@example.test", "password": "a-brand-new-password", "role": "buyer"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    reuse = client.post(
        "/password-reset/confirm",
        data={"token": token, "new_password": "another-password-1"},
        follow_redirects=False,
    )
    assert reuse.status_code == 400
    # the old password no longer works
    old = client.post(
        "/login",
        data={"email": "resetme@example.test", "password": TEST_PASSWORD, "role": "buyer"},
        follow_redirects=False,
    )
    assert old.status_code == 401


def test_tokens_never_reach_the_access_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SECRETTOKENVALUE123"
    with caplog.at_level(logging.INFO, logger="marketplace.access"):
        client.get(f"/verify?token={secret}")
        client.get(f"/password-reset/confirm?token={secret}")
        client.post("/verify", data={"token": secret})
        client.post("/password-reset/confirm", data={"token": secret, "new_password": "x" * 9})
    records = [r for r in caplog.records if r.name == "marketplace.access"]
    assert len(records) == 4
    for rec in records:
        assert secret not in rec.getMessage()
        assert secret not in repr(rec.__dict__)


def test_login_page_links_to_password_reset(client: TestClient) -> None:
    assert 'href="/password-reset"' in client.get("/login").text
