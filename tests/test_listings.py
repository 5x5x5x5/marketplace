"""Seller listing management + admin takedown (browse phase 1)."""

from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _onboarded_seller(client: TestClient, auth: AuthFactory, sid: str) -> dict[str, str]:
    onboard_and_avail(client, auth, sid, "s1")
    return auth("seller", "s1")


def _mk(client: TestClient, headers: dict[str, str], sid: str, **over: object) -> Response:
    body: dict[str, object] = {"service_type_id": sid, "title": "Deep clean"} | over
    return client.post("/v1/seller/listings", json=body, headers=headers)


def test_create_requires_onboarding(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = auth("seller", "s1")  # NOT onboarded
    r = _mk(client, seller, basic_service)
    assert r.status_code == 409
    assert "onboard" in r.json()["detail"].lower() or "payments" in r.json()["detail"].lower()


def test_create_and_list_own(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    r = _mk(
        client,
        seller,
        basic_service,
        description="Two rooms",
        photo_urls=["https://img.example/a.jpg"],
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["title"] == "Deep clean" and out["active"] is True and out["hidden"] is False
    mine = client.get("/v1/seller/listings", headers=seller).json()
    assert [x["id"] for x in mine] == [out["id"]]


def test_create_validation(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    assert _mk(client, seller, "no-such-type").status_code == 404
    assert (
        _mk(client, seller, basic_service, photo_urls=["http://insecure.example/a.jpg"]).status_code
        == 422
    )
    assert (
        _mk(
            client,
            seller,
            basic_service,
            photo_urls=[f"https://img.example/{i}.jpg" for i in range(13)],
        ).status_code
        == 422
    )
    assert _mk(client, seller, basic_service, title="").status_code == 422


def test_suspended_seller_cannot_create(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    assert (
        client.post(
            "/v1/admin/users/s1/suspend", json={"reason": "test"}, headers=admin
        ).status_code
        == 200
    )
    assert _mk(client, seller, basic_service).status_code == 403


def test_update_own_only_and_never_hidden(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    lid = _mk(client, seller, basic_service).json()["id"]
    r = client.put(
        f"/v1/seller/listings/{lid}",
        json={"title": "Deeper clean", "active": False},
        headers=seller,
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Deeper clean" and r.json()["active"] is False
    # hidden is admin-owned: a seller PUT must not accept/flip it
    r = client.put(f"/v1/seller/listings/{lid}", json={"hidden": True}, headers=seller)
    assert r.status_code in (200, 422)
    fresh = client.get("/v1/seller/listings", headers=seller).json()[0]
    assert fresh["hidden"] is False
    # not-yours 404
    other = auth("seller", "s2")
    client.post("/v1/seller/payments/onboard", headers=other)
    assert (
        client.put(
            f"/v1/seller/listings/{lid}", json={"title": "mine now"}, headers=other
        ).status_code
        == 404
    )


def test_admin_hide_unhide_audited(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    lid = _mk(client, seller, basic_service).json()["id"]
    assert client.post(f"/v1/admin/listings/{lid}/hide", headers=admin).status_code == 200
    assert client.get("/v1/seller/listings", headers=seller).json()[0]["hidden"] is True
    all_admin = client.get("/v1/admin/listings", headers=admin).json()
    assert any(x["id"] == lid and x["hidden"] for x in all_admin)
    assert client.post(f"/v1/admin/listings/{lid}/unhide", headers=admin).status_code == 200
    audit_rows = client.get("/v1/admin/audit", headers=admin).json()
    assert any(a["action"] == "hide_listing" for a in audit_rows)
    assert any(a["action"] == "unhide_listing" for a in audit_rows)
