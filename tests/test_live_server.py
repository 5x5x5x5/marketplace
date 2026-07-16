"""F2 regression: commits must complete before the response reaches the
client. These tests talk to a REAL uvicorn over a socket — see the
live_server fixture for why TestClient cannot test this class."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from marketplace import api
from tests.conftest import AuthFactory


def _start_second_server() -> tuple[str, uvicorn.Server, threading.Thread]:
    """A second, independently-scheduled uvicorn instance sharing the same DB.

    F2b's race requires the retry to be serviced by an event loop that isn't
    itself blocked by the first request's own (artificially slow) idempotency
    store commit — i.e. a second worker, the topology F2b was actually
    observed under. A same-process, single-event-loop retry against
    `live_server` alone cannot expose it: IdempotencyMiddleware's store commit
    is a synchronous DB call made directly on the event loop (no threadpool
    hop), so it blocks that loop for its own duration — which incidentally
    prevents that SAME loop from ever reading a same-connection retry until
    after the store commit has already finished, replay record and all.
    Confirmed by direct instrumentation: a concurrent /healthz poller saw a
    ~164ms stall exactly spanning the store commit, proving the loop really
    is blocked end-to-end, with no yield point where a same-loop retry could
    land early."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("second live server failed to start")
    return base, server, thread


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


def test_same_key_immediate_retry_replays_byte_identical(
    live_server: str, basic_service: str, auth: AuthFactory, slow_commits: dict[str, float]
) -> None:
    """F2b: the replay record must be durable before the client can retry.
    slow_commits makes the pre-fix failure deterministic: the record used to
    commit ~150ms after the response, so an immediate same-key retry against a
    second worker re-executed (observed live: the retry got a 404 where the
    first call made the job — quote already consumed). See
    `_start_second_server` for why the retry must land on a second worker."""
    base2, server2, thread2 = _start_second_server()
    try:
        buyer = auth("buyer", "alice")
        with (
            httpx.Client(base_url=live_server, timeout=10) as c1,
            httpx.Client(base_url=base2, timeout=10) as c2,
        ):
            q = c1.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
            key = {"Idempotency-Key": "f2b-retry-key"}
            first = c1.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer | key)
            second = c2.post("/v1/jobs", json={"quote_id": q.json()["id"]}, headers=buyer | key)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.text == first.text, "same-key retry did not replay byte-identical"
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)
