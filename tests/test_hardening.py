"""Body-size cap, TrustedHost/CORS wiring, admin-list pagination."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketplace import api
from marketplace.settings import settings
from tests.conftest import AuthFactory, Header


def test_oversized_body_413(client: TestClient, auth: AuthFactory) -> None:
    big = "x" * (settings.max_body_bytes + 1)
    r = client.post(
        "/v1/quotes",
        content=big.encode(),
        headers={**auth("buyer", "alice"), "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "request body too large"


def test_normal_body_passes(client: TestClient, auth: AuthFactory, basic_service: str) -> None:
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "alice")
    )
    assert r.status_code == 200


def test_oversized_413_not_stored_for_replay(client: TestClient, auth: AuthFactory) -> None:
    """The cap sits OUTSIDE idempotency: a 413 must not be replayable."""
    big = "x" * (settings.max_body_bytes + 1)
    headers = {
        **auth("buyer", "alice"),
        "Idempotency-Key": "cap-key-1",
        "Content-Type": "application/json",
    }
    assert client.post("/v1/quotes", content=big.encode(), headers=headers).status_code == 413
    from sqlalchemy import select

    from marketplace.db import SessionLocal
    from marketplace.entities import IdempotencyRecord

    with SessionLocal() as s:
        assert (
            s.scalars(select(IdempotencyRecord).where(IdempotencyRecord.key == "cap-key-1")).all()
            == []
        )


def test_chunked_oversized_body_413_not_stored(client: TestClient, auth: AuthFactory) -> None:
    """A chunked request (no Content-Length) that busts the cap must still
    413, not slip through as FastAPI's generic "error parsing the body" 400 —
    a 400 is < 500, so IdempotencyMiddleware would store it and poison the
    client's key forever. Sent via an iterator body so httpx uses
    Transfer-Encoding: chunked, exercising the counted (not declared-length)
    branch of BodySizeLimitMiddleware."""
    chunk = b"x" * 65536
    n_chunks = (settings.max_body_bytes // len(chunk)) + 2  # comfortably over the cap
    headers = {
        **auth("buyer", "alice"),
        "Idempotency-Key": "chunk-key-1",
        "Content-Type": "application/json",
    }
    request = client.build_request(
        "POST", "/v1/quotes", content=iter([chunk] * n_chunks), headers=headers
    )
    assert "content-length" not in request.headers  # genuinely chunked, not declared-length
    r = client.send(request)
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large"}

    from sqlalchemy import select

    from marketplace.db import SessionLocal
    from marketplace.entities import IdempotencyRecord

    with SessionLocal() as s:
        assert (
            s.scalars(select(IdempotencyRecord).where(IdempotencyRecord.key == "chunk-key-1")).all()
            == []
        )


def test_default_host_and_cors_are_open_and_absent(client: TestClient) -> None:
    r = client.get("/healthz", headers={"Host": "anything.example"})
    assert r.status_code == 200  # trusted_hosts defaults to *
    assert "access-control-allow-origin" not in r.headers  # CORS off by default


def test_hardening_wiring_respects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """_add_hardening_middleware reads settings: CORS only when configured,
    TrustedHost enforced when narrowed."""
    scratch = FastAPI()

    @scratch.get("/ping")
    def ping() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"pong": "yes"}

    monkeypatch.setattr(settings, "cors_origins", ["https://app.example"])
    monkeypatch.setattr(settings, "trusted_hosts", ["good.example"])
    api._add_hardening_middleware(scratch)  # pyright: ignore[reportPrivateUsage]
    c = TestClient(scratch, base_url="http://good.example")
    r = c.get("/ping", headers={"Origin": "https://app.example"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example"
    assert TestClient(scratch).get("/ping", headers={"Host": "evil.example"}).status_code == 400


def test_admin_lists_paginate(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    a1 = auth("buyer", "alice")
    a2 = auth("buyer", "bob")
    client.get("/v1/profile", headers=a1)
    client.get("/v1/profile", headers=a2)  # materialize two buyer profiles
    full = client.get("/v1/admin/buyers", headers=admin).json()
    assert len(full) >= 2
    page = client.get("/v1/admin/buyers?limit=1", headers=admin).json()
    assert len(page) == 1
    page2 = client.get("/v1/admin/buyers?limit=1&offset=1", headers=admin).json()
    assert page2 and page2[0] != page[0]
    # reviews + reports accept the params too (empty lists are fine)
    assert client.get("/v1/admin/reviews/buyer?limit=1", headers=admin).status_code == 200
    assert client.get("/v1/admin/reports?limit=1", headers=admin).status_code == 200
