"""F2 regression: commits must complete before the response reaches the
client. These tests talk to a REAL uvicorn over a socket — see the
live_server fixture for why TestClient cannot test this class."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AuthFactory


def test_commit_lands_before_client_has_response(
    live_server: str, basic_service: str, auth: AuthFactory, slow_commits: dict[str, float]
) -> None:
    buyer = auth("buyer", "alice")
    # auth() above also goes through the (already-patched) slow Session.commit
    # to create the buyer row; clear that unrelated timestamp so the box can
    # only be populated by the commit belonging to the request under test —
    # otherwise a still-in-flight commit reads as "done" via a stale value.
    slow_commits.clear()
    with httpx.Client(base_url=live_server, timeout=10) as c:
        r = c.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
        received = time.monotonic()
    assert r.status_code == 200, r.text
    assert "last_commit_done" in slow_commits, (
        "no commit was recorded for this request by the time the client received its "
        "response (F2): the response reached the client before the transaction committed"
    )
    assert slow_commits["last_commit_done"] < received, (
        "the client held the response before its transaction committed (F2): "
        f"commit finished {slow_commits['last_commit_done'] - received:+.3f}s after receipt"
    )


def test_read_your_writes_over_socket(
    live_server: str, basic_service: str, auth: AuthFactory
) -> None:
    """The exact field repro shape: a fresh quote must be usable immediately
    (pre-fix this 404d ~2/30 on chained calls)."""
    buyer = auth("buyer", "alice")
    with httpx.Client(base_url=live_server, timeout=10) as c:
        for i in range(100):
            q = c.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
            assert q.status_code == 200, q.text
            j = c.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer)
            assert j.status_code == 200, f"iteration {i}: fresh quote invisible: {j.text}"
            c.post(f"/v1/jobs/{j.json()['id']}/cancel", headers=buyer)


def test_failed_commit_returns_500_envelope_and_persists_nothing(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """A commit failure must be a truthful 500, never a lying 200 (in-process
    is fine here — failure semantics, not ordering, are under test)."""
    from fastapi import Request
    from sqlalchemy import func, select
    from sqlalchemy.exc import OperationalError

    from marketplace import api
    from marketplace.db import SessionLocal, get_session
    from marketplace.entities import Quote

    buyer = auth("buyer", "alice")

    def failing_get_session(request: Request):
        session = SessionLocal()
        request.state.db_session = session

        def boom() -> None:
            raise OperationalError("induced commit failure", None, Exception("induced"))

        session.commit = boom  # type: ignore[method-assign]
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[get_session] = failing_get_session
    try:
        r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    finally:
        api.app.dependency_overrides.clear()
    assert r.status_code == 500
    assert r.json()["detail"] == "internal error"
    assert "request_id" in r.json()
    assert "x-request-id" in r.headers
    with SessionLocal() as s:
        assert (s.scalar(select(func.count()).select_from(Quote)) or 0) == 0


def test_commit_route_invariant_catches_rogue_router() -> None:
    from fastapi import APIRouter, FastAPI

    from marketplace import api

    rogue_app = FastAPI()
    rogue = APIRouter(prefix="/v1")

    @rogue.get("/rogue")
    def rogue_route() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    rogue_app.include_router(rogue)
    with pytest.raises(RuntimeError, match="CommitRoute"):
        api._assert_commit_routes(rogue_app)  # pyright: ignore[reportPrivateUsage]
