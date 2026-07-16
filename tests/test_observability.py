"""Request ids, access log, JSON formatter, and the 500 envelope."""

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marketplace import api
from marketplace.observability import JsonFormatter, request_id_var
from tests.conftest import AuthFactory


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/healthz")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)


def test_inbound_request_id_honored(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("bad", ["x" * 65, "bad\nnewline", "smuggl\x00null"])
def test_hostile_request_id_regenerated(client: TestClient, bad: str) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": bad})
    assert r.headers["x-request-id"] != bad
    assert len(r.headers["x-request-id"]) == 32


def test_access_log_line_shape_and_redaction(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="marketplace.access"):
        client.get("/v1/admin/config?secret=topsecret", headers={"X-Request-ID": "rid-1"})
    records = [r for r in caplog.records if r.name == "marketplace.access"]
    assert len(records) == 1
    rec: Any = records[0]
    assert rec.method == "GET"
    assert rec.path == "/v1/admin/config"  # no query string, ever
    assert isinstance(rec.status, int)
    assert rec.duration_ms >= 0
    assert request_id_var.get() != ""  # contextvar machinery alive
    assert "topsecret" not in rec.getMessage()


def test_healthz_not_access_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="marketplace.access"):
        client.get("/healthz")
    assert not [r for r in caplog.records if r.name == "marketplace.access"]


def test_json_formatter_shape() -> None:
    fmt = JsonFormatter()
    rec = logging.LogRecord(
        "marketplace.test", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    line = json.loads(fmt.format(rec))
    assert line["msg"] == "hello world"
    assert line["level"] == "INFO"
    assert line["logger"] == "marketplace.test"
    assert "ts" in line and "request_id" in line


def test_envelope_hides_internals(caplog: pytest.LogCaptureFixture) -> None:
    """An unhandled exception becomes a clean 500 with the request id; the
    traceback goes to the log, never the body."""

    @api.app.get("/v1/_test_boom")
    def _boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("kaboom-sentinel")

    try:
        with caplog.at_level(logging.ERROR, logger="marketplace"):
            c = TestClient(api.app)
            r = c.get("/v1/_test_boom", headers={"X-Request-ID": "boom-rid"})
        assert r.status_code == 500
        assert r.json() == {"detail": "internal error", "request_id": "boom-rid"}
        assert r.headers["x-request-id"] == "boom-rid"
        assert "kaboom-sentinel" not in r.text
        logged = "".join(
            (r.message or "") + (str(r.exc_text) if r.exc_text else "") for r in caplog.records
        )
        assert "kaboom-sentinel" in logged  # traceback IS in the log
    finally:
        api.app.router.routes[:] = [
            route
            for route in api.app.router.routes
            if getattr(route, "path", "") != "/v1/_test_boom"
        ]


def test_http_exceptions_not_enveloped(client: TestClient, auth: AuthFactory) -> None:
    """404s/422s keep their FastAPI shapes; only unhandled errors are enveloped."""
    r = client.get("/v1/nope-does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] != "internal error"
    assert "x-request-id" in r.headers
