"""Client Idempotency-Key semantics on money-mutating POSTs."""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace.db import SessionLocal
from marketplace.entities import IdempotencyRecord, Payment
from tests.conftest import TEST_PASSWORD, AuthFactory, Header
from tests.test_payments import new_job, onboard_and_avail


def _idem(headers: Header, key: str) -> Header:
    return {**headers, "Idempotency-Key": key}


def test_replayed_accept_returns_stored_response_and_charges_once(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    seller = _idem(auth("seller", "s1"), "accept-once")
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]

    r1 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    r2 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()  # byte-for-byte replay, not a re-execution (which would 409)
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 1


def test_same_key_different_path_conflicts(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    buyer = _idem(auth("buyer", "alice"), "one-key")
    r1 = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r1.status_code == 201
    r2 = client.post("/v1/jobs", json={"quote_id": r1.json()["id"]}, headers=buyer)
    assert r2.status_code == 409


def test_keys_are_scoped_per_principal(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    a = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        headers=_idem(auth("buyer", "alice"), "k"),
    )
    b = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        headers=_idem(auth("buyer", "bob"), "k"),
    )
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]  # not a replay across principals


def test_error_responses_replay_too(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    """A stored error replays even after the world changes - proof of replay, not re-execution."""
    buyer = _idem(auth("buyer", "alice"), "bad-quote")
    r1 = client.post("/v1/quotes", json={"service_type_id": "nope"}, headers=buyer)
    assert r1.status_code == 404
    # Make the identical request valid: a re-executed call would now return 200.
    client.put(
        "/v1/admin/config/service_types/nope",
        json={"base_buyer_price": 20, "base_seller_payout": 14},
        headers=admin,
    )
    client.put("/v1/admin/config/pipelines/nope", json={"buyer": [], "seller": []}, headers=admin)
    r2 = client.post("/v1/quotes", json={"service_type_id": "nope"}, headers=buyer)
    assert r2.status_code == 404  # replayed from the store
    assert r1.json() == r2.json()


def test_oversized_key_rejected(client: TestClient, auth: AuthFactory) -> None:
    r = client.post(
        "/v1/quotes",
        json={"service_type_id": "x"},
        headers=_idem(auth("buyer", "alice"), "k" * 201),
    )
    assert r.status_code == 422


def test_no_auth_passes_through_to_401(client: TestClient) -> None:
    r = client.post(
        "/v1/quotes", json={"service_type_id": "x"}, headers={"Idempotency-Key": "anon"}
    )
    assert r.status_code == 401


def test_auth_paths_never_stored_even_with_key_and_bearer(client: TestClient) -> None:
    """Auth responses carry raw bearer tokens: a client that attaches
    Authorization globally AND stamps every POST with an Idempotency-Key must
    not get its login/signup response captured into idempotency_keys."""
    signup = client.post(
        "/v1/auth/signup",
        json={
            "email": "idem@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Idem",
        },
        headers={"Idempotency-Key": "blanket-key"},
    )
    assert signup.status_code == 201
    session_token = signup.json()["token"]

    creds = {"email": "idem@example.test", "password": TEST_PASSWORD, "role": "buyer"}
    stamped = {"Authorization": f"Bearer {session_token}", "Idempotency-Key": "login-key"}
    login = client.post("/v1/auth/login", json=creds, headers=stamped)
    assert login.status_code == 200
    login_token = login.json()["token"]

    with SessionLocal() as s:
        rows = s.scalars(select(IdempotencyRecord)).all()
    assert all(login_token not in row.response_body for row in rows)
    assert all(session_token not in row.response_body for row in rows)
    assert all(not row.path.startswith("/v1/auth/") for row in rows)

    # A second identical login executes for real — a fresh session, not a replay.
    again = client.post("/v1/auth/login", json=creds, headers=stamped)
    assert again.status_code == 200
    assert again.json()["token"] != login_token


def test_store_failure_never_fails_a_committed_operation(
    client: TestClient,
    basic_service: str,
    auth: AuthFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The domain work commits before the record store; a store failure must
    log and still send the success response — a missed replay record beats
    500ing a successful operation.

    Patches IdempotencyRecord.__init__ only, not the module symbol: the early
    replay-lookup SELECT also references IdempotencyRecord and must survive
    unchanged, so only the later store-side construction should explode."""
    from marketplace.entities import IdempotencyRecord

    def boom_init(self: object, **_: object) -> None:
        raise RuntimeError("store exploded (induced)")

    monkeypatch.setattr(IdempotencyRecord, "__init__", boom_init)
    buyer = auth("buyer", "alice")
    q = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    with caplog.at_level(logging.WARNING, logger="marketplace.idempotency"):
        r = client.post(
            "/v1/jobs",
            json={"quote_id": q.json()["id"]},
            headers=buyer | {"Idempotency-Key": "boom-key"},
        )
    assert r.status_code == 201, r.text  # the job was created and committed
    assert any("idempotency store failed" in rec.getMessage() for rec in caplog.records)
