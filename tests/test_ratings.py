"""Ratings: buyer reviews a completed job; the seller's rating drives matching."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def _run_job_to_completion(
    client: TestClient, auth: AuthFactory, sid: str, seller: str, buyer: str
) -> str:
    """Full flow through completion. Returns the job id."""
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    job_id = client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer)).json()[
        "id"
    ]
    offer_id = client.get("/v1/seller/offers", headers=auth("seller", seller)).json()[0]["id"]
    client.post(f"/v1/seller/offers/{offer_id}/accept", headers=auth("seller", seller))
    client.post(f"/v1/seller/jobs/{job_id}/complete", headers=auth("seller", seller))
    return job_id


def test_review_updates_seller_rating(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _run_job_to_completion(client, auth, basic_service, "carol", "alice")
    r = client.post(f"/v1/jobs/{job_id}/review", json={"rating": 4}, headers=auth("buyer", "alice"))
    assert r.status_code == 200

    profile = client.get("/v1/seller/profile", headers=auth("seller", "carol")).json()
    assert profile["rating"] == 4.0
    assert profile["rating_count"] == 1


def test_cannot_review_twice(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _run_job_to_completion(client, auth, basic_service, "carol", "alice")
    buyer = auth("buyer", "alice")
    assert (
        client.post(f"/v1/jobs/{job_id}/review", json={"rating": 5}, headers=buyer).status_code
        == 200
    )
    assert (
        client.post(f"/v1/jobs/{job_id}/review", json={"rating": 1}, headers=buyer).status_code
        == 409
    )


def test_cannot_review_incomplete_job(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    client.post(
        "/v1/seller/availability",
        json={"service_type_id": basic_service},
        headers=auth("seller", "s1"),
    )
    qid = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "alice")
    ).json()["id"]
    job_id = client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", "alice")).json()[
        "id"
    ]
    r = client.post(f"/v1/jobs/{job_id}/review", json={"rating": 5}, headers=auth("buyer", "alice"))
    assert r.status_code == 409  # not completed


def test_rating_feeds_highest_rated_matching(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    # carol completes a job and is reviewed 5; dave is unrated.
    job_id = _run_job_to_completion(client, auth, basic_service, "carol", "alice")
    client.post(f"/v1/jobs/{job_id}/review", json={"rating": 5}, headers=auth("buyer", "alice"))
    client.post(
        "/v1/seller/availability",
        json={"service_type_id": basic_service},
        headers=auth("seller", "dave"),
    )
    client.put(
        "/v1/admin/config/matching_strategy", json={"strategy": "highest_rated"}, headers=admin
    )

    qid = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "bob")
    ).json()["id"]
    client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", "bob"))
    # carol (rated 5) is offered over dave (unrated).
    assert client.get("/v1/seller/offers", headers=auth("seller", "carol")).json()
    assert not client.get("/v1/seller/offers", headers=auth("seller", "dave")).json()
