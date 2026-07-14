"""Seller→buyer reviews: mirror of the buyer→seller review, display-only aggregate."""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from marketplace import api
from marketplace.db import SessionLocal
from marketplace.entities import Adjustment, BuyerProfile, Dispute, Job
from marketplace.models import AdjustmentKind, DisputeSource
from tests.conftest import IS_POSTGRES, AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def test_seller_reviews_table_registered() -> None:
    from marketplace.entities import Base

    assert "seller_reviews" in Base.metadata.tables


def test_buyer_profile_rating_property() -> None:
    prof = BuyerProfile(id="b1")
    assert prof.rating is None
    prof.rating_count = 2
    prof.rating_sum = 7
    assert prof.rating == 3.5


def test_adjustments_amount_check_rejects_negative() -> None:
    """DB-level backstop for the ledger doctrine: amounts are positive, kind
    carries the sign. Enforced by CHECK on both backends."""

    with SessionLocal() as s:
        job = Job(
            quote_id=uuid4(),
            service_type_id="svc",
            buyer_id="b1",
            buyer_price=Decimal("10.00"),
        )
        s.add(job)
        s.flush()
        dispute = Dispute(job_id=job.id, source=DisputeSource.BUYER, buyer_id="b1", reason="x")
        s.add(dispute)
        s.flush()
        s.add(
            Adjustment(
                job_id=job.id,
                dispute_id=dispute.id,
                kind=AdjustmentKind.REFUND,
                amount=Decimal("-1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def _completed_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str = "alice") -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, buyer)
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    return str(job["id"])


def test_seller_reviews_buyer_happy_path_and_aggregate(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    r = client.post(
        f"/v1/seller/jobs/{job_id}/review",
        json={"rating": 4, "comment": "prompt payment"},
        headers=auth("seller", "s1"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buyer_id"] == "alice"
    assert body["rating"] == 4
    assert "seller_id" not in body  # mirror of ReviewOut: author id not echoed

    # Second job, second review: aggregate is the running mean.
    job2 = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job2['id']}/complete", headers=auth("seller", "s1"))
    r = client.post(
        f"/v1/seller/jobs/{job2['id']}/review", json={"rating": 1}, headers=auth("seller", "s1")
    )
    assert r.status_code == 200

    from marketplace.entities import BuyerProfile

    with SessionLocal() as s:
        prof = s.get(BuyerProfile, "alice")
        assert prof is not None
        assert prof.rating_count == 2
        assert prof.rating_sum == 5
        assert prof.rating == 2.5


def test_review_unknown_job_404(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    r = client.post(
        f"/v1/seller/jobs/{uuid4()}/review", json={"rating": 5}, headers=auth("seller", "s1")
    )
    assert r.status_code == 404


def test_review_not_own_job_404(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    r = client.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=auth("seller", "other")
    )
    assert r.status_code == 404


def test_review_incomplete_job_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # ACCEPTED, not COMPLETED
    r = client.post(
        f"/v1/seller/jobs/{job['id']}/review", json={"rating": 5}, headers=auth("seller", "s1")
    )
    assert r.status_code == 409


def test_review_duplicate_409(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    assert (
        client.post(
            f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=seller
        ).status_code
        == 200
    )
    r = client.post(f"/v1/seller/jobs/{job_id}/review", json={"rating": 1}, headers=seller)
    assert r.status_code == 409


def test_review_schema_bounds(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    for bad in ({"rating": 0}, {"rating": 6}, {"rating": 3, "comment": "x" * 2001}):
        r = client.post(f"/v1/seller/jobs/{job_id}/review", json=bad, headers=seller)
        assert r.status_code == 422, bad


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_review_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """Two threads race the same review; UNIQUE(job_id) is the backstop and the
    loser must get the sequential path's 409, not a 500."""
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(
            f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=seller
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [200, 409], codes

    from marketplace.entities import SellerReview

    with SessionLocal() as s:
        assert len(s.scalars(select(SellerReview)).all()) == 1


def test_buyer_profile_surface(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    r = client.get("/v1/profile", headers=buyer)
    assert r.status_code == 200
    assert r.json() == {"id": "alice", "rating": None, "rating_count": 0, "completed_jobs": 0}

    job_id = _completed_job(client, auth, basic_service)
    client.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 4}, headers=auth("seller", "s1")
    )
    body = client.get("/v1/profile", headers=buyer).json()
    assert body["rating"] == 4.0
    assert body["rating_count"] == 1
    assert body["completed_jobs"] == 1


def test_admin_buyers_list(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    client.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=auth("seller", "s1")
    )
    r = client.get("/v1/admin/buyers", headers=admin)
    assert r.status_code == 200
    rows = {b["id"]: b for b in r.json()}
    assert rows["alice"]["rating"] == 5.0
    # Role guard: a buyer token cannot read the admin list.
    assert client.get("/v1/admin/buyers", headers=auth("buyer", "alice")).status_code == 403
