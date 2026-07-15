"""Moderation: suspension, content takedown, reports. Spec: 2026-07-14-moderation-design.md."""

from fastapi.testclient import TestClient

from marketplace.models import PaymentStatus
from marketplace.payments.fake import FakeProvider
from tests.conftest import AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def test_moderation_schema_registered() -> None:
    from marketplace.entities import Base

    assert "reports" in Base.metadata.tables
    users = Base.metadata.tables["users"]
    assert "status" in users.c and "suspended_reason" in users.c and "suspended_at" in users.c
    assert "comment_hidden" in Base.metadata.tables["reviews"].c
    assert "comment_hidden" in Base.metadata.tables["seller_reviews"].c


def test_public_comment_property_is_the_invariant_home() -> None:
    """Non-admin serializations read public_comment; hiding nulls it, nothing else."""
    from marketplace.entities import Review, SellerReview

    for cls in (Review, SellerReview):
        row = cls(rating=3, comment="rude text")
        assert row.public_comment == "rude text"
        row.comment_hidden = True
        assert row.public_comment is None
        assert row.comment == "rude text"  # the row itself is untouched
        row.comment_hidden = False
        assert row.public_comment == "rude text"


def _suspend(
    client: TestClient, admin: Header, user_id: str, reason: str = "abuse"
) -> dict[str, object]:
    r = client.post(f"/v1/admin/users/{user_id}/suspend", json={"reason": reason}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def test_suspend_lifecycle(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("buyer", "alice")  # materialize the user row
    body = _suspend(client, admin, "alice")
    assert body["status"] == "suspended"
    assert body["suspended_reason"] == "abuse"
    assert body["suspended_at"] is not None
    # double-suspend -> 409
    assert (
        client.post(
            "/v1/admin/users/alice/suspend", json={"reason": "x"}, headers=admin
        ).status_code
        == 409
    )
    r = client.post("/v1/admin/users/alice/reinstate", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["suspended_reason"] is None
    # double-reinstate -> 409
    assert client.post("/v1/admin/users/alice/reinstate", headers=admin).status_code == 409
    # unknown -> 404
    assert (
        client.post(
            "/v1/admin/users/nobody/suspend", json={"reason": "x"}, headers=admin
        ).status_code
        == 404
    )


def test_admins_cannot_be_suspended(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("admin", "root2")
    r = client.post("/v1/admin/users/root2/suspend", json={"reason": "x"}, headers=admin)
    assert r.status_code == 422


def test_suspended_buyer_verbs(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    fake_payments: FakeProvider,
) -> None:
    """Acquisition 403s; exit verbs and reads still work (freeze-new/finish-in-flight)."""
    # Job stays AWAITING_PAYMENT (not ACCEPTED) so the buyer-cancel exit verb is
    # actually reachable — cancel_job only permits PENDING/AWAITING_PAYMENT, by
    # design, independent of suspension (see test_buyer_still_cannot_cancel_accepted).
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    buyer = auth("buyer", "alice")
    _suspend(client, admin, "alice")

    r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
    # reads still work
    assert client.get("/v1/jobs", headers=buyer).status_code == 200
    assert client.get("/v1/profile", headers=buyer).status_code == 200
    # exit verb still works: cancel the awaiting-payment job
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=buyer).status_code == 200


def test_suspended_seller_verbs(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Seller acquisition 403s; complete (finish verb) still works."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    seller = auth("seller", "s1")
    _suspend(client, admin, "s1")

    assert (
        client.post(
            "/v1/seller/availability", json={"service_type_id": basic_service}, headers=seller
        ).status_code
        == 403
    )
    # finish verb allowed: complete the in-flight job
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=seller)
    assert r.status_code == 200
    # gated after completion too: reviewing the buyer is acquisition
    r = client.post(f"/v1/seller/jobs/{job['id']}/review", json={"rating": 1}, headers=seller)
    assert r.status_code == 403


def test_suspended_seller_leaves_matching(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Job.status stays "pending" either way (offers are separate rows) — the
    observable is the seller's offers list."""
    onboard_and_avail(client, auth, basic_service, "s1")
    _suspend(client, admin, "s1")
    new_job(client, auth, basic_service, "alice")
    assert client.get("/v1/seller/offers", headers=auth("seller", "s1")).json() == []

    # reinstate -> the next job creation matches again
    client.post("/v1/admin/users/s1/reinstate", headers=admin)
    new_job(client, auth, basic_service, "alice")
    assert len(client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()) >= 1
