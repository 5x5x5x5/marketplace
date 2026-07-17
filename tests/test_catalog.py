"""Public catalog with live indicative pricing (browse phase 2). Unauthenticated —
no test in this file ever sends an Authorization header to a /v1/catalog/* route."""

from typing import Any

from fastapi.testclient import TestClient

from marketplace.db import SessionLocal
from marketplace.entities import SellerProfile
from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _seller(client: TestClient, auth: AuthFactory, sid: str) -> Header:
    """An onboarded (payments_ready) seller — no availability needed for the
    catalog, which is visibility-only, not matching."""
    headers = auth("seller", sid)
    r = client.post("/v1/seller/payments/onboard", headers=headers)
    assert r.status_code == 200
    return headers


def _listing(client: TestClient, headers: Header, sid: str, **over: object) -> dict[str, Any]:
    body: dict[str, object] = {"service_type_id": sid, "title": "Deep clean"} | over
    r = client.post("/v1/seller/listings", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_catalog_empty_ok(client: TestClient) -> None:
    r = client.get("/v1/catalog/listings")
    assert r.status_code == 200
    assert r.json() == []


def test_catalog_shows_bookable_only(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    # A: active listing, active+onboarded seller — visible.
    a_headers = _seller(client, auth, "sa")
    listing_a = _listing(client, a_headers, basic_service)

    # B: listing itself deactivated by its seller.
    b_headers = _seller(client, auth, "sb")
    listing_b = _listing(client, b_headers, basic_service)
    r = client.put(
        f"/v1/seller/listings/{listing_b['id']}", json={"active": False}, headers=b_headers
    )
    assert r.status_code == 200

    # C: hidden by admin takedown.
    c_headers = _seller(client, auth, "sc")
    listing_c = _listing(client, c_headers, basic_service)
    assert (
        client.post(f"/v1/admin/listings/{listing_c['id']}/hide", headers=admin).status_code == 200
    )

    # D: seller suspended AFTER the listing was created.
    d_headers = _seller(client, auth, "sd")
    listing_d = _listing(client, d_headers, basic_service)
    assert (
        client.post(
            "/v1/admin/users/sd/suspend", json={"reason": "test"}, headers=admin
        ).status_code
        == 200
    )

    # E: seller onboarded at listing-creation time, then flipped un-onboarded
    # (white-box — payments_ready otherwise only flips via the account webhook).
    e_headers = _seller(client, auth, "se")
    listing_e = _listing(client, e_headers, basic_service)
    with SessionLocal() as s:
        prof = s.get(SellerProfile, "se")
        assert prof is not None
        prof.payments_ready = False
        s.commit()

    listed = client.get("/v1/catalog/listings").json()
    assert [x["id"] for x in listed] == [listing_a["id"]]

    for lid, visible in (
        (listing_a["id"], True),
        (listing_b["id"], False),
        (listing_c["id"], False),
        (listing_d["id"], False),
        (listing_e["id"], False),
    ):
        r = client.get(f"/v1/catalog/listings/{lid}")
        assert r.status_code == (200 if visible else 404), (lid, r.status_code)

    # Seller profile 404s exactly on {unknown, suspended, not-onboarded} — a
    # seller whose LISTING merely isn't visible (b, c) still resolves, just
    # with an empty listings array (the account itself is fine).
    assert client.get("/v1/catalog/sellers/sa").status_code == 200
    assert client.get("/v1/catalog/sellers/sd").status_code == 404  # suspended
    assert client.get("/v1/catalog/sellers/se").status_code == 404  # not onboarded
    assert client.get("/v1/catalog/sellers/nope").status_code == 404  # unknown

    for sid in ("sb", "sc"):
        r = client.get(f"/v1/catalog/sellers/{sid}")
        assert r.status_code == 200
        assert r.json()["listings"] == []


def test_catalog_price_from_matches_quote(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _seller(client, auth, "sp")
    listing = _listing(client, seller, basic_service)
    # A healthy, available candidate elsewhere in the pool: spread (20 - 14 = 6)
    # is well above the required margin (floor 0 + ~0.88 fee estimate), so the
    # buyer-price floor bump never engages — a clean mirror comparison.
    onboard_and_avail(client, auth, basic_service, "scand")

    buyer = auth("buyer", "bp")
    quote = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()

    catalog_listing = client.get(f"/v1/catalog/listings/{listing['id']}").json()
    assert catalog_listing["price_from"] == quote["buyer_price"]


_FORBIDDEN = {"seller_payout", "margin", "capacity", "email", "payments_ready", "tier"}

type _JSON = None | bool | int | float | str | list["_JSON"] | dict[str, "_JSON"]


def _keys(obj: _JSON) -> set[str]:
    """Recursive key-scan: every dict key anywhere in a JSON-decoded body."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _keys(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _keys(item)
    return found


def test_catalog_asymmetry_keyscan(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _seller(client, auth, "sk")
    listing = _listing(client, seller, basic_service)

    listed = client.get("/v1/catalog/listings").json()
    detail = client.get(f"/v1/catalog/listings/{listing['id']}").json()
    profile = client.get("/v1/catalog/sellers/sk").json()

    found = _keys(listed) | _keys(detail) | _keys(profile)
    assert not (found & _FORBIDDEN), found & _FORBIDDEN


def test_catalog_seller_profile_shape(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller_headers = _seller(client, auth, "ss")
    listing = _listing(client, seller_headers, basic_service)
    onboard_and_avail(client, auth, basic_service, "ss")  # idempotent onboard + adds availability

    buyer = auth("buyer", "bs")
    qid = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer).json()[
        "id"
    ]
    job = client.post("/v1/jobs", json={"quote_id": qid}, headers=buyer).json()
    offer_id = client.get("/v1/seller/offers", headers=seller_headers).json()[0]["id"]
    assert (
        client.post(f"/v1/seller/offers/{offer_id}/accept", headers=seller_headers).status_code
        == 200
    )
    assert (
        client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=seller_headers).status_code
        == 200
    )
    r = client.post(
        f"/v1/jobs/{job['id']}/review", json={"rating": 5, "comment": "great"}, headers=buyer
    )
    assert r.status_code == 201, r.text

    profile = client.get("/v1/catalog/sellers/ss").json()
    assert profile["seller_id"] == "ss"
    assert profile["display_name"] == "ss"  # the auth fixture sets display_name = sub
    assert profile["rating"] == 5.0
    assert profile["rating_count"] == 1
    assert profile["completed_jobs"] == 1
    assert [x["id"] for x in profile["listings"]] == [listing["id"]]
