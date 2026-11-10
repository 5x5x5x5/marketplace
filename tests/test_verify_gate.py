"""The opt-in email-verification gate (enforced in the services, so the JSON
API and the pages behave the same), plus the verify and reset pages."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Offer, Quote, User
from marketplace.models import OfferStatus
from marketplace.settings import settings
from tests.conftest import AuthFactory
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
