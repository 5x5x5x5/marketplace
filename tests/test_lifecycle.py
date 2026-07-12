"""Job lifecycle: offer expiry + re-match, decline, cancel, graceful no-seller, history."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from marketplace.db import SessionLocal
from marketplace.entities import Offer
from tests.conftest import AuthFactory, Header


def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )


def _make_job(
    client: TestClient, auth: AuthFactory, sid: str, buyer: str = "alice"
) -> dict[str, object]:
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    return client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer)).json()


def _offered_to(client: TestClient, auth: AuthFactory, names: list[str]) -> str:
    for n in names:
        if client.get("/v1/seller/offers", headers=auth("seller", n)).json():
            return n
    return ""


def _backdate_open_offer(job_id: str) -> None:
    """Force the current open offer to have already expired (white-box)."""
    with SessionLocal() as s:
        offer = s.query(Offer).filter(Offer.job_id == UUID(job_id), Offer.status == "offered").one()
        offer.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()


def test_no_seller_yields_expired_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """A quote with no eligible seller produces an EXPIRED job (200), not a bare error."""
    job = _make_job(client, auth, basic_service)  # no availability posted
    assert job["status"] == "expired"
    assert job["seller_id"] is None


def test_decline_advances_to_next_seller(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service, "a")
    _available(client, auth, basic_service, "b")
    _make_job(client, auth, basic_service)

    first = _offered_to(client, auth, ["a", "b"])
    other = "b" if first == "a" else "a"
    offer_id = client.get("/v1/seller/offers", headers=auth("seller", first)).json()[0]["id"]

    r = client.post(f"/v1/seller/offers/{offer_id}/decline", headers=auth("seller", first))
    assert r.status_code == 200
    # Re-matched to the other seller; the decliner no longer has an open offer.
    assert client.get("/v1/seller/offers", headers=auth("seller", other)).json()
    assert not client.get("/v1/seller/offers", headers=auth("seller", first)).json()


def test_offer_expiry_rematches(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service, "a")
    _available(client, auth, basic_service, "b")
    job = _make_job(client, auth, basic_service)
    first = _offered_to(client, auth, ["a", "b"])
    other = "b" if first == "a" else "a"

    _backdate_open_offer(str(job["id"]))
    # Trigger the lazy sweep (admin sweep, or any offer read).
    assert client.post("/v1/admin/jobs/sweep", headers=admin).status_code == 200
    assert client.get("/v1/seller/offers", headers=auth("seller", other)).json()


def test_offer_expiry_with_no_alternative_expires_job(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service, "solo")
    job = _make_job(client, auth, basic_service)
    _backdate_open_offer(str(job["id"]))
    client.post("/v1/admin/jobs/sweep", headers=admin)

    body = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert body["status"] == "expired"


def test_buyer_cancel_pending_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service, "a")
    job = _make_job(client, auth, basic_service)
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # The seller's offer was closed.
    assert not client.get("/v1/seller/offers", headers=auth("seller", "a")).json()


def test_cannot_cancel_accepted_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service, "a")
    job = _make_job(client, auth, basic_service)
    offer_id = client.get("/v1/seller/offers", headers=auth("seller", "a")).json()[0]["id"]
    client.post(f"/v1/seller/offers/{offer_id}/accept", headers=auth("seller", "a"))
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 409


def test_buyer_and_seller_job_history(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _available(client, auth, basic_service, "a")
    job = _make_job(client, auth, basic_service, "alice")
    offer_id = client.get("/v1/seller/offers", headers=auth("seller", "a")).json()[0]["id"]
    client.post(f"/v1/seller/offers/{offer_id}/accept", headers=auth("seller", "a"))

    buyer_jobs = client.get("/v1/jobs", headers=auth("buyer", "alice")).json()
    assert len(buyer_jobs) == 1 and buyer_jobs[0]["id"] == job["id"]

    seller_jobs = client.get("/v1/seller/jobs", headers=auth("seller", "a")).json()
    assert len(seller_jobs) == 1 and seller_jobs[0]["status"] == "accepted"
    assert "buyer_price" not in seller_jobs[0]
