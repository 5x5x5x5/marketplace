"""Server-rendered pages: catalog grid, shared-logic extraction, invariant."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _listed_seller(
    client: TestClient, auth: AuthFactory, sid: str, title: str = "City rides"
) -> None:
    onboard_and_avail(client, auth, sid, "s1")
    r = client.post(
        "/v1/seller/listings",
        json={"service_type_id": sid, "title": title, "description": "Vetted drivers"},
        headers=auth("seller", "s1"),
    )
    assert r.status_code == 201, r.text


def test_landing_renders_catalog(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "City rides" in r.text
    assert "from" in r.text  # price_from label
    assert "20.00" in r.text


def test_landing_empty_ok(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200


def test_landing_hides_invisible(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _listed_seller(client, auth, basic_service)
    lid = client.get("/v1/seller/listings", headers=auth("seller", "s1")).json()[0]["id"]
    assert client.post(f"/v1/admin/listings/{lid}/hide", headers=admin).status_code == 200
    assert "City rides" not in client.get("/").text


def test_page_source_asymmetry(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    _listed_seller(client, auth, basic_service)
    html = client.get("/").text.lower()
    for forbidden in ("seller_payout", "margin", "capacity", "payments_ready", "@"):
        assert forbidden not in html, forbidden


def test_xss_listing_title_escaped(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _listed_seller(client, auth, basic_service, title="<script>alert(1)</script>")
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_static_served(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_commit_route_invariant_covers_pages() -> None:
    import pytest
    from fastapi import APIRouter, FastAPI

    from marketplace import api

    rogue_app = FastAPI()
    rogue = APIRouter()  # NOT /v1 — the widened invariant must still catch it

    @rogue.get("/rogue-page")
    def rogue_page() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    rogue_app.include_router(rogue)
    with pytest.raises(RuntimeError, match="CommitRoute"):
        api._assert_commit_routes(rogue_app)  # pyright: ignore[reportPrivateUsage]
