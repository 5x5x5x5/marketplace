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


def _signup_cookie(client: TestClient, email: str, headers: Header | None = None) -> str:
    r = client.post(
        "/signup",
        data={
            "email": email,
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Cookie Buyer",
        },
        headers=headers or {},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r.cookies["session_token"]


def test_cookie_only_post_is_idempotent(client: TestClient, basic_service: str) -> None:
    """I1: peek_principal must resolve the session_token cookie too, not just
    the Authorization header — a cookie-only POST with an Idempotency-Key
    must replay the same quote on a retry, not re-execute (Phase 3's booking
    forms are cookie+key)."""
    token = _signup_cookie(client, "idem-cookie@example.test")
    key = {"Idempotency-Key": "cookie-quote-key"}
    r1 = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        cookies={"session_token": token},
        headers=key,
    )
    r2 = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        cookies={"session_token": token},
        headers=key,
    )
    assert r1.status_code == r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["id"] == r2.json()["id"]  # replayed, not re-executed


def test_header_principal_wins_over_cookie_for_idempotency_storage(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """A request carrying both a valid header and a valid (different-user)
    cookie must store under the HEADER principal — same header-first order
    as auth._resolve_principal, not a new resolution rule."""
    cookie_token = _signup_cookie(client, "idem-cookie-2@example.test")
    header = auth("buyer", "alice") | {"Idempotency-Key": "header-wins-key"}
    r = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        cookies={"session_token": cookie_token},
        headers=header,
    )
    assert r.status_code == 201, r.text
    with SessionLocal() as s:
        row = s.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == "header-wins-key"))
    assert row is not None
    assert row.principal == "buyer:alice"  # header identity, not the cookie's


def test_page_signup_never_stored_even_with_bearer_and_key(
    client: TestClient, auth: AuthFactory
) -> None:
    """I2: /signup (pages.py) issues a raw session token via Set-Cookie and a
    303 redirect; a stored replay would omit both, breaking the redirect. It
    must never be captured — even with a stray Authorization header attached
    (mirrors test_auth_paths_never_stored_even_with_key_and_bearer for
    /v1/auth/*)."""
    stray_bearer = auth("buyer", "alice")
    r = client.post(
        "/signup",
        data={
            "email": "page-idem-signup@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Page Idem",
        },
        headers=stray_bearer | {"Idempotency-Key": "page-signup-key"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        rows = s.scalars(select(IdempotencyRecord)).all()
    assert all(row.path != "/signup" for row in rows)


def test_page_login_never_stored_even_with_key(client: TestClient) -> None:
    """I2: /login (pages.py) likewise must never be captured."""
    client.post(
        "/signup",
        data={
            "email": "page-idem-login@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Page Idem Login",
        },
        follow_redirects=False,
    )
    r = client.post(
        "/login",
        data={
            "email": "page-idem-login@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
        },
        headers={"Idempotency-Key": "page-login-key"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        rows = s.scalars(select(IdempotencyRecord)).all()
    assert all(row.path != "/login" for row in rows)


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
