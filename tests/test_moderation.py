"""Moderation: suspension, content takedown, reports. Spec: 2026-07-14-moderation-design.md."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Report, SellerProfile
from marketplace.mail import RecordingEmailSender
from marketplace.models import PaymentStatus
from marketplace.payments.fake import FakeProvider
from tests.conftest import IS_POSTGRES, AuthFactory, Header
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
    # Quote obtained BEFORE suspension, spent AFTER — isolates create_job's own
    # guard from create_quote's (already covered below).
    spare_quote_id = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=buyer
    ).json()["id"]
    _suspend(client, admin, "alice")

    r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
    r = client.post("/v1/jobs", json={"quote_id": spare_quote_id}, headers=buyer)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
    # reads still work
    assert client.get("/v1/jobs", headers=buyer).status_code == 200
    assert client.get("/v1/profile", headers=buyer).status_code == 200
    # exit verb still works: cancel the awaiting-payment job
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=buyer).status_code == 200


def test_suspended_buyer_review_and_dispute_gated(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """review_job and open_dispute (both buyer acquisition-adjacent verbs) 403
    while the buyer is suspended, once there's a completed job to act on."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    buyer = auth("buyer", "alice")
    _suspend(client, admin, "alice")

    r = client.post(f"/v1/jobs/{job['id']}/review", json={"rating": 5}, headers=buyer)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
    r = client.post(
        f"/v1/jobs/{job['id']}/dispute", json={"reason": "not as described"}, headers=buyer
    )
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"


def test_suspended_seller_onboarding_gated(
    client: TestClient, auth: AuthFactory, admin: Header
) -> None:
    """onboard_payments 403s while the seller is suspended."""
    seller = auth("seller", "s9")
    _suspend(client, admin, "s9")
    r = client.post("/v1/seller/payments/onboard", headers=seller)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"


def test_suspended_seller_verbs(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Seller acquisition 403s; complete (finish verb) still works."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    # A second job while job's offer is still open — both land as OFFERED
    # (matching only counts ACCEPTED/AWAITING_PAYMENT against capacity), so we
    # can accept one now and leave the other's offer open to test accept_offer
    # against below. Offer ids fetched BEFORE suspending (GETs stay open).
    other_job = new_job(client, auth, basic_service, "bob")
    offers = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()
    offer_id = next(o["id"] for o in offers if o["job_id"] == job["id"])
    other_offer_id = next(o["id"] for o in offers if o["job_id"] == other_job["id"])
    client.post(f"/v1/seller/offers/{offer_id}/accept", headers=auth("seller", "s1"))
    seller = auth("seller", "s1")
    _suspend(client, admin, "s1")

    assert (
        client.post(
            "/v1/seller/availability", json={"service_type_id": basic_service}, headers=seller
        ).status_code
        == 403
    )
    # accept_offer gated too: the other job's offer is still open
    r = client.post(f"/v1/seller/offers/{other_offer_id}/accept", headers=seller)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
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


def _reviewed_job(client: TestClient, basic_service: str, auth: AuthFactory) -> str:
    """Completed job where alice reviewed s1 (buyer-kind review). Returns job id."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    r = client.post(
        f"/v1/jobs/{job['id']}/review",
        json={"rating": 2, "comment": "rude and late"},
        headers=auth("buyer", "alice"),
    )
    assert r.status_code == 201, r.text
    return str(job["id"])


def test_hide_and_unhide_review(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)
    listed = client.get("/v1/admin/reviews/buyer", headers=admin).json()
    assert len(listed) == 1
    review = listed[0]
    assert review["author_id"] == "alice" and review["subject_id"] == "s1"
    assert review["comment"] == "rude and late" and review["comment_hidden"] is False

    r = client.post(f"/v1/admin/reviews/buyer/{review['id']}/hide", headers=admin)
    assert r.status_code == 200
    assert r.json()["comment_hidden"] is True
    assert r.json()["comment"] == "rude and late"  # admin still sees the text
    # idempotence guard
    assert (
        client.post(f"/v1/admin/reviews/buyer/{review['id']}/hide", headers=admin).status_code
        == 409
    )
    # aggregate untouched by hiding
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.rating_count == 1 and prof.rating_sum == 2

    r = client.post(f"/v1/admin/reviews/buyer/{review['id']}/unhide", headers=admin)
    assert r.status_code == 200 and r.json()["comment_hidden"] is False
    assert (
        client.post(f"/v1/admin/reviews/buyer/{review['id']}/unhide", headers=admin).status_code
        == 409
    )


def test_hide_seller_review_kind(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _reviewed_job(client, basic_service, auth)
    client.post(
        f"/v1/seller/jobs/{job_id}/review",
        json={"rating": 1, "comment": "bad buyer"},
        headers=auth("seller", "s1"),
    )
    listed = client.get("/v1/admin/reviews/seller", headers=admin).json()
    assert len(listed) == 1
    assert listed[0]["author_id"] == "s1" and listed[0]["subject_id"] == "alice"
    r = client.post(f"/v1/admin/reviews/seller/{listed[0]['id']}/hide", headers=admin)
    assert r.status_code == 200
    # unknown id -> 404 (valid UUID, no row)
    assert client.post(f"/v1/admin/reviews/seller/{uuid4()}/hide", headers=admin).status_code == 404


def test_reset_display_name(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("buyer", "alice")
    r = client.post("/v1/admin/users/alice/reset_display_name", headers=admin)
    assert r.status_code == 200
    assert r.json()["display_name"] == "user-" + "alice"[:8]
    assert (
        client.post("/v1/admin/users/nobody/reset_display_name", headers=admin).status_code == 404
    )


def _report(
    client: TestClient, headers: Header, kind: str, target: str, reason: str = "abusive"
) -> Response:
    return client.post(
        "/v1/reports",
        json={"target_kind": kind, "target_id": target, "reason": reason},
        headers=headers,
    )


def test_report_eligibility_matrix(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)  # alice <-> s1 share a job
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    review_id = client.get("/v1/admin/reviews/buyer", headers=admin).json()[0]["id"]

    # counterparty user: OK
    r = _report(client, seller, "user", "alice")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "open"
    # duplicate: 409
    assert _report(client, seller, "user", "alice").status_code == 409
    # stranger: 403
    auth("buyer", "bob")
    assert _report(client, auth("seller", "s1"), "user", "bob").status_code == 403
    # self: 422
    assert _report(client, buyer, "user", "alice").status_code == 422
    # unknown user: 404
    assert _report(client, seller, "user", "ghost").status_code == 404
    # review subject reports the review: OK
    assert _report(client, seller, "review", review_id).status_code == 201
    # review author reports own review ("take my comment down"): OK
    assert _report(client, buyer, "review", review_id).status_code == 201
    # unrelated party on the review: 403
    assert _report(client, auth("buyer", "bob"), "review", review_id).status_code == 403
    # malformed review id: 404
    assert _report(client, seller, "seller_review", "not-a-uuid").status_code == 404
    # admin bearer cannot file: 403
    assert _report(client, admin, "user", "alice").status_code == 403


def test_suspended_reporter_cannot_file(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)
    _suspend(client, admin, "s1")
    r = _report(client, auth("seller", "s1"), "user", "alice")
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"


def test_report_views_and_resolve(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    admin: Header,
    mail_outbox: RecordingEmailSender,
) -> None:
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    report_id = _report(client, seller, "user", "alice").json()["id"]

    # admin was notified on create (RecordingEmailSender.sent is a list of
    # (to, subject, body) tuples — mail.py:33)
    from marketplace.notifications import drain_once

    drain_once(mail_outbox)
    assert any("Report filed" in sent[1] for sent in mail_outbox.sent)

    # reporter view: status only, no admin prose
    mine = client.get("/v1/reports", headers=seller).json()
    assert len(mine) == 1 and "resolution_note" not in mine[0]

    # admin filter + resolve
    assert len(client.get("/v1/admin/reports?status=open", headers=admin).json()) == 1
    assert client.get("/v1/admin/reports?status=bogus", headers=admin).status_code == 422
    r = client.post(
        f"/v1/admin/reports/{report_id}/resolve",
        json={"status": "actioned", "note": "hid the comment"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "actioned" and r.json()["resolved_at"] is not None
    # terminal: 409
    assert (
        client.post(
            f"/v1/admin/reports/{report_id}/resolve", json={"status": "dismissed"}, headers=admin
        ).status_code
        == 409
    )
    # reporter now sees the status, still not the note
    mine = client.get("/v1/reports", headers=seller).json()
    assert mine[0]["status"] == "actioned" and "resolution_note" not in mine[0]


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_report_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return _report(c, seller, "user", "alice").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [201, 409], codes
    with SessionLocal() as s:
        assert len(s.scalars(select(Report)).all()) == 1


def _idem(headers: Header, key: str) -> Header:
    return {**headers, "Idempotency-Key": key}


def test_idempotency_does_not_replay_stale_suspension_403(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A reversible 403 (suspension) must never be cached: reinstate, then a
    replay of the SAME Idempotency-Key must execute for real, not return the
    stale 403 forever."""
    buyer = auth("buyer", "alice")
    _suspend(client, admin, "alice")
    keyed = _idem(buyer, "quote-attempt-1")
    r1 = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=keyed)
    assert r1.status_code == 403 and r1.json()["detail"] == "account suspended"

    client.post("/v1/admin/users/alice/reinstate", headers=admin)

    r2 = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=keyed)
    assert r2.status_code == 201, r2.text


def test_idempotency_does_not_replay_stale_report_403(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    _suspend(client, admin, "s1")
    keyed = _idem(seller, "report-attempt-1")
    r1 = _report(client, keyed, "user", "alice")
    assert r1.status_code == 403

    client.post("/v1/admin/users/s1/reinstate", headers=admin)

    r2 = _report(client, keyed, "user", "alice")
    assert r2.status_code == 201, r2.text


def test_report_canonicalizes_uuid_variants(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Case/hyphen/urn variants of the same review UUID must all collide on
    the same canonical target_id — no UNIQUE evasion, no duplicate rows."""
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    review_id = client.get("/v1/admin/reviews/buyer", headers=admin).json()[0]["id"]

    r1 = _report(client, seller, "review", review_id)
    assert r1.status_code == 201, r1.text
    r2 = _report(client, seller, "review", review_id.upper())
    assert r2.status_code == 409, r2.text

    with SessionLocal() as s:
        rows = s.scalars(select(Report)).all()
        assert len(rows) == 1
        assert rows[0].target_id == review_id.lower()


def _double_reviewed_job(client: TestClient, basic_service: str, auth: AuthFactory) -> str:
    """Completed job with BOTH a buyer->seller review and a seller->buyer
    review, seller/buyer comments distinguishable. Returns job id."""
    job_id = _reviewed_job(client, basic_service, auth)
    r = client.post(
        f"/v1/seller/jobs/{job_id}/review",
        json={"rating": 4, "comment": "prompt buyer"},
        headers=auth("seller", "s1"),
    )
    assert r.status_code == 201, r.text
    return job_id


def test_job_reviews_seller_can_discover_and_report_buyer_review(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _double_reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    reviews = client.get(f"/v1/seller/jobs/{job_id}/reviews", headers=seller).json()
    assert len(reviews) == 2
    for row in reviews:
        assert "buyer_id" not in row and "seller_id" not in row
    by_kind = {r["kind"]: r for r in reviews}
    assert by_kind["review"]["rating"] == 2
    assert by_kind["review"]["comment"] == "rude and late"
    assert by_kind["seller_review"]["rating"] == 4
    assert by_kind["seller_review"]["comment"] == "prompt buyer"

    # end-to-end: the id+kind pair is directly reportable
    r = client.post(
        "/v1/reports",
        json={
            "target_kind": by_kind["review"]["kind"],
            "target_id": by_kind["review"]["id"],
            "reason": "abusive language",
        },
        headers=seller,
    )
    assert r.status_code == 201, r.text


def test_job_reviews_buyer_symmetric(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _double_reviewed_job(client, basic_service, auth)
    buyer = auth("buyer", "alice")
    reviews = client.get(f"/v1/jobs/{job_id}/reviews", headers=buyer).json()
    assert len(reviews) == 2
    for row in reviews:
        assert "buyer_id" not in row and "seller_id" not in row
    by_kind = {r["kind"]: r for r in reviews}
    r = client.post(
        "/v1/reports",
        json={
            "target_kind": by_kind["seller_review"]["kind"],
            "target_id": by_kind["seller_review"]["id"],
            "reason": "abusive language",
        },
        headers=buyer,
    )
    assert r.status_code == 201, r.text


def test_job_reviews_non_party_gets_404(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _double_reviewed_job(client, basic_service, auth)
    onboard_and_avail(client, auth, basic_service, "s2")
    other_seller = auth("seller", "s2")
    assert client.get(f"/v1/seller/jobs/{job_id}/reviews", headers=other_seller).status_code == 404
    other_buyer = auth("buyer", "bob")
    assert client.get(f"/v1/jobs/{job_id}/reviews", headers=other_buyer).status_code == 404


def test_job_reviews_hidden_comment_reads_null_rating_stays(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _reviewed_job(client, basic_service, auth)
    review_id = client.get("/v1/admin/reviews/buyer", headers=admin).json()[0]["id"]
    client.post(f"/v1/admin/reviews/buyer/{review_id}/hide", headers=admin)

    seller = auth("seller", "s1")
    reviews = client.get(f"/v1/seller/jobs/{job_id}/reviews", headers=seller).json()
    assert len(reviews) == 1
    assert reviews[0]["comment"] is None
    assert reviews[0]["rating"] == 2


def test_job_reviews_empty_list_when_none_yet(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    seller = auth("seller", "s1")
    assert client.get(f"/v1/seller/jobs/{job['id']}/reviews", headers=seller).json() == []
