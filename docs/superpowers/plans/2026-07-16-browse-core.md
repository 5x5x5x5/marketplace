# Browse Core Implementation Plan (market-platform fork, phase 1)

> **Note (added later):** this phase was built in a separate fork named `market-platform`, which was later merged back. The phase is part of this repository; the fork, `upstream`/`origin` remotes and fork-specific database names mentioned below no longer exist.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dispatch template into a browse marketplace's backend: seller listings, an unauthenticated public catalog with live indicative pricing, chosen-seller (pinned) booking that never substitutes sellers, and human-paced timing defaults — with dispatch mode fully intact underneath.

**Architecture:** `Listing` is presentation over existing service types (pricing stays platform-owned — a listing never carries a price). A pinned quote stores `listing_id`/`pinned_seller_id`; the pin travels onto the Job (quotes are deleted at job creation); `_match_and_offer` gains a pin guard so decline/expiry EXPIRE pinned jobs instead of re-matching. Public catalog router computes `price_from` through the pure pricing core. Visibility = `active` (seller-owned) AND NOT `hidden` (admin-owned) AND seller bookable.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic (migration #11), Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-browse-core-design.md` (approved under standing authorization).

## Global Constraints

- `uv` only. **Bare exit codes** — never gate on piped commands.
- Pyright strict 0 across `src/` and `tests/`; ruff clean.
- **PostToolUse formatter strips not-yet-used imports** — import + first use in the SAME edit.
- TDD binding and audited: failing tests first, RUN them, quote red output in the report.
- **Fork Postgres URL: `postgresql+psycopg://marketplace:marketplace@localhost:5432/market_platform`** (the `market_platform` database inside the SHARED `marketplace-db-1` container). **NEVER run `docker compose down -v`** — the container also serves the template's database and its live dev server. **Never bind port 8000.**
- Every new router sets `route_class=CommitRoute` (the import-time invariant will fail collection otherwise — that's the signal you forgot).
- All money math through `models.to_money`; floor checks through `matching.required_spread` (never gross).
- Commit trailer: `Co-Authored-By: <agent attribution trailer>`. No backticks in double-quoted `git commit -m`. Commit ONLY listed files.
- Migrations render plain types, no app imports; `down_revision = "87aec6283afd"`; chain after this branch: exactly 11.
- Work on branch `browse-core` (exists, spec committed).

---

### Task 1: Listing entity, migration #11, seller CRUD, admin moderation

**Files:**
- Modify: `src/marketplace/entities.py` (Listing after `SellerProfile` ~line 135; Quote pin columns ~line 172; Job pin column in the Job class right after it)
- Modify: `src/marketplace/models.py` (Listing schemas after `SellerCompletionOut` ~line 218)
- Modify: `src/marketplace/api.py` (seller endpoints in the seller_router region ~line 995+; admin endpoints after `admin_list_reviews`)
- Create: `migrations/versions/<autogen>_listings.py`
- Test: `tests/test_listings.py` (new)

**Interfaces:**
- Consumes: `_require_active`, `repo.get_or_create_seller` (has `.payments_ready`), `_paginate`/`Limit`/`Offset`, `audit`, house 404-not-yours pattern.
- Produces (Tasks 2-3 rely on): `entities.Listing` (fields per spec: seller_id, service_type_id, title, description, photo_urls, active, hidden, timestamps); `Quote.listing_id: uuid.UUID | None` + `Quote.pinned_seller_id: str | None`; `Job.pinned_seller_id: str | None`; `models.SellerListingOut/ListingCreate/ListingUpdate`; routes `POST|GET /v1/seller/listings`, `PUT /v1/seller/listings/{id}`, `GET /v1/admin/listings`, `POST /v1/admin/listings/{id}/hide|unhide`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_listings.py`:

```python
"""Seller listing management + admin takedown (browse phase 1)."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header
from tests.test_payments import onboard_and_avail


def _onboarded_seller(client: TestClient, auth: AuthFactory, sid: str) -> dict[str, str]:
    onboard_and_avail(client, auth, sid, "s1")
    return auth("seller", "s1")


def _mk(client: TestClient, headers: dict[str, str], sid: str, **over: object) -> "object":
    body: dict[str, object] = {"service_type_id": sid, "title": "Deep clean"} | over
    return client.post("/v1/seller/listings", json=body, headers=headers)


def test_create_requires_onboarding(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    seller = auth("seller", "s1")  # NOT onboarded
    r = _mk(client, seller, basic_service)
    assert r.status_code == 409
    assert "onboard" in r.json()["detail"].lower() or "payments" in r.json()["detail"].lower()


def test_create_and_list_own(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    r = _mk(client, seller, basic_service, description="Two rooms", photo_urls=["https://img.example/a.jpg"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["title"] == "Deep clean" and out["active"] is True and out["hidden"] is False
    mine = client.get("/v1/seller/listings", headers=seller).json()
    assert [x["id"] for x in mine] == [out["id"]]


def test_create_validation(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    assert _mk(client, seller, "no-such-type").status_code == 404
    assert _mk(client, seller, basic_service, photo_urls=["http://insecure.example/a.jpg"]).status_code == 422
    assert _mk(client, seller, basic_service, photo_urls=[f"https://img.example/{i}.jpg" for i in range(13)]).status_code == 422
    assert _mk(client, seller, basic_service, title="").status_code == 422


def test_suspended_seller_cannot_create(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    assert client.post("/v1/admin/users/s1/suspend", json={"reason": "test"}, headers=admin).status_code == 200
    assert _mk(client, seller, basic_service).status_code == 403


def test_update_own_only_and_never_hidden(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    seller = _onboarded_seller(client, auth, basic_service)
    lid = _mk(client, seller, basic_service).json()["id"]
    r = client.put(f"/v1/seller/listings/{lid}", json={"title": "Deeper clean", "active": False}, headers=seller)
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
    assert client.put(f"/v1/seller/listings/{lid}", json={"title": "mine now"}, headers=other).status_code == 404


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
```

Adaptation notes: mirror the suspend call shape from `tests/test_moderation.py` (`_suspend` helper — it sends `json={"reason": ...}`); the `hidden`-in-PUT expectation is a design point — the update model simply has no `hidden` field, so extra fields are either ignored (200) or rejected (422) depending on the model config; both satisfy the invariant asserted (hidden stays False). Type annotations per house style.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_listings.py -v
```

Expected: 404s (routes missing). Capture.

- [ ] **Step 3: Implement — entities.py**

After `SellerProfile` (same import block already has everything needed):

```python
class Listing(Base):
    """A seller's public offering of a service type — presentation only.
    Pricing stays platform-owned: a listing never carries a price. `active`
    is the seller's toggle; `hidden` is an admin takedown the seller cannot
    undo (moderation parity with review comments)."""

    __tablename__ = "listings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    service_type_id: Mapped[str] = mapped_column(ForeignKey("service_types.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(4000), default="")
    photo_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(default=True)
    hidden: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)
```

`Quote` gains (after `expires_at`):

```python
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("listings.id"), default=None)
    pinned_seller_id: Mapped[str | None] = mapped_column(String(128), default=None)
```

`Job` gains (after its `seller_id` column):

```python
    pinned_seller_id: Mapped[str | None] = mapped_column(String(128), default=None)
```

- [ ] **Step 4: Implement — models.py**

After `SellerCompletionOut`:

```python
class ListingCreate(BaseModel):
    service_type_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    photo_urls: list[str] = Field(default_factory=list[str], max_length=12)

    @field_validator("photo_urls")
    @classmethod
    def _https_only(cls, v: list[str]) -> list[str]:
        for url in v:
            if not url.startswith("https://") or len(url) > 512:
                raise ValueError("photo_urls must be https:// and at most 512 chars")
        return v


class ListingUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    photo_urls: list[str] | None = Field(default=None, max_length=12)
    active: bool | None = None

    _https_only = field_validator("photo_urls")(ListingCreate._https_only.__func__)  # type: ignore[arg-type]


class SellerListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_type_id: str
    title: str
    description: str
    photo_urls: list[str]
    active: bool
    hidden: bool
    created_at: datetime
    updated_at: datetime
```

If the shared-validator trick fights pyright strict, duplicate the 5-line validator into `ListingUpdate` instead — duplication beats a type-ignore here (implementer's call, note it). Import `field_validator` with its first use.

- [ ] **Step 5: Implement — api.py seller + admin endpoints**

Seller region (after the seller review endpoint; imports `Listing`, `ListingCreate`, `ListingUpdate`, `SellerListingOut` in the same edits):

```python
@seller_router.post("/listings", response_model=SellerListingOut, status_code=201)
def create_listing(body: ListingCreate, session: SessionDep, seller_id: SellerId) -> Listing:
    _require_active(session, seller_id)
    if session.get(ServiceType, body.service_type_id) is None:
        raise HTTPException(status_code=404, detail="unknown service_type_id")
    seller = repo.get_or_create_seller(session, seller_id)
    if not seller.payments_ready:
        raise HTTPException(status_code=409, detail="onboard payments before listing")
    listing = Listing(
        seller_id=seller_id,
        service_type_id=body.service_type_id,
        title=body.title,
        description=body.description,
        photo_urls=body.photo_urls,
    )
    session.add(listing)
    session.flush()
    return listing


@seller_router.get("/listings", response_model=list[SellerListingOut])
def my_listings(session: SessionDep, seller_id: SellerId) -> list[Listing]:
    rows = session.scalars(
        select(Listing).where(Listing.seller_id == seller_id).order_by(Listing.created_at.desc())
    ).all()
    return list(rows)


@seller_router.put("/listings/{listing_id}", response_model=SellerListingOut)
def update_listing(
    listing_id: UUID, body: ListingUpdate, session: SessionDep, seller_id: SellerId
) -> Listing:
    _require_active(session, seller_id)
    listing = session.get(Listing, listing_id)
    if listing is None or listing.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="listing not found")
    for field in ("title", "description", "photo_urls", "active"):
        value = getattr(body, field)
        if value is not None:
            setattr(listing, field, value)
    return listing
```

Admin region:

```python
@admin_router.get("/listings", response_model=list[SellerListingOut])
def admin_list_listings(session: SessionDep, limit: Limit = 100, offset: Offset = 0) -> list[Listing]:
    rows = session.scalars(select(Listing).order_by(Listing.created_at.desc())).all()
    return _paginate(list(rows), limit, offset)


@admin_router.post("/listings/{listing_id}/hide", response_model=SellerListingOut)
def hide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")
    listing.hidden = True
    audit(session, admin_id, "hide_listing", str(listing_id), {})
    return listing


@admin_router.post("/listings/{listing_id}/unhide", response_model=SellerListingOut)
def unhide_listing(listing_id: UUID, session: SessionDep, admin_id: AdminId) -> Listing:
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")
    listing.hidden = False
    audit(session, admin_id, "unhide_listing", str(listing_id), {})
    return listing
```

- [ ] **Step 6: Migration #11**

```bash
uv run alembic revision -m "listings"
```

Style-match `87aec6283afd_ops_indexes.py`; `down_revision = "87aec6283afd"`. Upgrade: `op.create_table("listings", ...)` (plain types: `sa.Uuid()`, `sa.String(length=...)`, `sa.JSON()`, `sa.Boolean()`, `sa.DateTime()`, FKs to users/service_types, indexes on seller_id + service_type_id), then `op.add_column("quotes", sa.Column("listing_id", sa.Uuid(), sa.ForeignKey("listings.id"), nullable=True))`, `op.add_column("quotes", sa.Column("pinned_seller_id", sa.String(length=128), nullable=True))`, `op.add_column("jobs", sa.Column("pinned_seller_id", sa.String(length=128), nullable=True))`. Downgrade in exact reverse order.

- [ ] **Step 7: Gates**

```bash
uv run pytest tests/test_listings.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/market_platform uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/market_platform uv run pytest
```

All exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/entities.py src/marketplace/models.py src/marketplace/api.py migrations/versions/ tests/test_listings.py
git commit -m "Add seller listings with admin takedown (browse phase 1, migration 11)"
```

---

### Task 2: Public catalog with live indicative pricing

**Files:**
- Modify: `src/marketplace/models.py` (catalog schemas after `SellerListingOut`)
- Modify: `src/marketplace/api.py` (new `catalog_router` near the other router definitions; registered in the include block; one pricing helper)
- Test: `tests/test_catalog.py` (new)

**Interfaces:**
- Consumes: Task 1's `Listing`; `User`/`UserStatus`, `SellerProfile` (rating fields, `payments_ready`, `completed_jobs`, `display_name` lives on `User` — VERIFY where display_name lives and adapt), `repo.load_pricing_config`, `repo.available_count`, `repo.active_demand`, `run_pipeline`, `PricingContext`/`Side`, `to_money`, `CommitRoute`.
- Produces (Task 3/phase 2 rely on): `GET /v1/catalog/listings[?service_type_id=]`, `GET /v1/catalog/listings/{id}`, `GET /v1/catalog/sellers/{seller_id}` — all UNAUTHENTICATED; `models.CatalogListingOut {id, title, description, photo_urls, service_type_id, price_from, seller}` with `CatalogSellerOut {seller_id, display_name, rating, rating_count}`; `api._indicative_buyer_price(session, service_type_id) -> Decimal | None`; `api._visible_listings_stmt()` select for `active AND NOT hidden AND seller ACTIVE AND payments_ready`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog.py` with these behaviors (write real code, mirroring house fixtures; intents binding):

1. `test_catalog_empty_ok` — no auth header, `GET /v1/catalog/listings` → 200 `[]`.
2. `test_catalog_shows_bookable_only` — build the visibility matrix: listing A (active, visible seller) appears; B (active=False) absent; C (hidden by admin) absent; D (seller suspended after listing) absent; E (seller onboarded=False — construct via white-box flip of `payments_ready`) absent. Check list, detail (404 for each invisible), and seller profile (only A's seller resolves; suspended seller's profile 404s).
3. `test_catalog_price_from_matches_quote` — with a healthy spread (no floor bump), `price_from` for the listing equals the `buyer_price` of a quote created at the same moment (string-equal money).
4. `test_catalog_asymmetry_keyscan` — recursive key-scan (copy the `_keys` helper idiom from the information-asymmetry tests if present, else write the 8-line version) over all three catalog responses: none of `{"seller_payout", "margin", "capacity", "email", "payments_ready", "tier"}` appear anywhere.
5. `test_catalog_seller_profile_shape` — display name, rating aggregate (seed one review via the normal flow), completed_jobs, and the seller's visible listings inline.

- [ ] **Step 2: Run to verify failure** (`uv run pytest tests/test_catalog.py -v` → 404s; capture)

- [ ] **Step 3: Implement — models.py**

```python
class CatalogSellerOut(BaseModel):
    seller_id: str
    display_name: str
    rating: float | None
    rating_count: int


class CatalogListingOut(BaseModel):
    id: UUID
    title: str
    description: str
    photo_urls: list[str]
    service_type_id: str
    price_from: Decimal  # indicative: the buyer pipeline now; a real quote may bump upward
    seller: CatalogSellerOut


class CatalogSellerProfileOut(BaseModel):
    seller_id: str
    display_name: str
    rating: float | None
    rating_count: int
    completed_jobs: int
    listings: list[CatalogListingOut]
```

- [ ] **Step 4: Implement — api.py**

Router (with the other routers; register in the include block — the CommitRoute invariant enforces the route_class):

```python
catalog_router = APIRouter(prefix="/v1/catalog", tags=["catalog"], route_class=CommitRoute)
```

Pricing helper (near the quote handler; mirrors its computation — keep the two in sight of each other with a cross-comment):

```python
def _indicative_buyer_price(session: Session, service_type_id: str) -> Decimal | None:
    """Catalog 'from' price: the buyer pipeline at this instant, pre-floor.
    Mirrors create_quote's price computation; a real quote may floor-bump up."""
    cfg = repo.load_pricing_config(session, service_type_id)
    if cfg is None:
        return None
    supply = repo.available_count(session, service_type_id)
    demand = repo.active_demand(session, service_type_id) + 1
    ctx = PricingContext(side=Side.BUYER, buyer_completed_jobs=0, live_supply=supply, live_demand=demand)
    return to_money(run_pipeline(float(cfg.service.base_buyer_price), cfg.buyer_pipeline, ctx, cfg.adjuster_params))
```

Visibility + endpoints: one select joining `Listing`→`User` (status ACTIVE) →`SellerProfile` (payments_ready), reused by all three endpoints; per-request memo dict for `price_from` by service_type_id (compute once per type per response). Build `CatalogSellerOut` from the joined User/SellerProfile rows (rating math per the existing profile endpoints' idiom — find and reuse it). Paginate the list endpoint with `Limit`/`Offset`.

VERIFY during implementation: where `display_name` and the rating aggregate actually live (User vs profile tables) and reuse the existing public-profile computation rather than re-deriving; note in the report.

- [ ] **Step 5-6: Gates + commit** (same gate block as Task 1; commit `src/marketplace/models.py src/marketplace/api.py tests/test_catalog.py` with message "Add public catalog with live indicative pricing")

---

### Task 3: Chosen-seller booking + human-paced TTLs

**Files:**
- Modify: `src/marketplace/models.py` (`QuoteRequest` ~line 525, `QuoteOut` ~line 136)
- Modify: `src/marketplace/api.py` (`create_quote` ~line 557, `create_job` ~line 621, `_match_and_offer` ~line 214)
- Modify: `src/marketplace/repo.py` (`candidate_for_seller` helper)
- Modify: `src/marketplace/settings.py` (TTL defaults)
- Test: `tests/test_pinned_booking.py` (new)

**Interfaces:**
- Consumes: Tasks 1-2 (`Listing`, visibility semantics); `seller_payout_for`, `required_spread`, `_create_offer(session, job, seller_id, seller_payout)`, `_expire_unmatched(session, job)`, `Availability`.
- Produces: `QuoteRequest {service_type_id: str | None, listing_id: UUID | None}` with the validation matrix; `QuoteOut` + nullable `listing_id`/`pinned_seller_id`; `repo.candidate_for_seller(session, seller_id, service_type_id) -> Candidate | None` (profile-based, availability-independent — for the pinned floor probe); pinned create_job path; `_match_and_offer` pin guard; settings `quote_ttl_minutes=15`, `offer_ttl_minutes=120`.

- [ ] **Step 1: Write the failing tests** — `tests/test_pinned_booking.py`, real code, these intents binding:

1. `test_pinned_offer_lands_on_chosen_seller` — TWO onboarded+available sellers where the strategy (cheapest_payout) would pick the OTHER one (give the chosen seller a higher-payout tier via the admin tier endpoint + `seller_tier_multiplier` params, mirroring `tests/test_matching.py`'s `_tiered_pipeline`); buyer quotes with `listing_id` of the expensive seller's listing → job → the offer's `seller_id` is the PINNED seller (poll `/v1/seller/offers` as both). Proves matching was bypassed, not coincidence.
2. `test_pinned_floor_probes_pinned_payout` — floor configured such that the CHEAP rival would clear it but the pinned seller's payout would not → the pinned quote's `buyer_price` is bumped (higher than an unpinned quote's price at the same moment). Assert both prices; derive expected bump per the `required_spread` + whole-unit walk (same derivation style as `tests/test_margin_floor.py`).
3. `test_quote_validation_matrix` — listing_id only → 200/201 with `service_type_id` filled from the listing and `pinned_seller_id` set in `QuoteOut`; both agreeing → OK; both disagreeing → 422; neither → 422.
4. `test_quote_409_when_unavailable` — each reason: inactive listing, hidden listing, suspended seller, payments_ready=False → 409 "listing unavailable".
5. `test_job_410_when_listing_dies_between_quote_and_job` — quote pinned OK → seller deactivates listing → `POST /v1/jobs` → 410; quote consumed (second attempt 404); NO job/offer/payment rows created.
6. `test_pinned_decline_expires_never_rematches` — two sellers; pinned offer declined → job `expired`; rival NEVER receives an offer for it.
7. `test_pinned_offer_expiry_expires_job` — white-box the offer's `expires_at` into the past (house idiom from the lifecycle tests), trigger the sweep (`POST /v1/admin/jobs/sweep`), job → `expired`, no second offer.
8. `test_dispatch_unchanged` — no `listing_id` → strategy path picks cheapest as before (and the full existing suite is the wider regression net).
9. `test_ttl_defaults` — `settings.quote_ttl_minutes == 15` and `settings.offer_ttl_minutes == 120` (guards the fork divergence against upstream merges).

- [ ] **Step 2: Red run** (capture; tests 1-7,9 must fail, 8 passes)

- [ ] **Step 3: Implement — models + repo**

`QuoteRequest`: `service_type_id: str | None = Field(default=None, min_length=1, max_length=128)`, `listing_id: UUID | None = None`. `QuoteOut` gains `listing_id: UUID | None` and `pinned_seller_id: str | None` (from_attributes covers the entity columns).

`repo.candidate_for_seller` (build a `Candidate` from `SellerProfile` regardless of availability — the pinned floor probe needs the payout math even though availability is checked at job time; mirror `eligible_candidates`'s Candidate construction for a single seller; return None if no profile).

`settings.py`: change the two defaults with a comment: `# fork divergence: browse marketplace with human sellers — see browse-core spec`.

- [ ] **Step 4: Implement — create_quote pinned branch**

At the top of `create_quote` (replacing the current unconditional config load), full code:

```python
    _require_active(session, buyer_id)
    listing: Listing | None = None
    if req.listing_id is not None:
        listing = session.get(Listing, req.listing_id)
        if listing is None or not _listing_bookable(session, listing):
            raise HTTPException(status_code=409, detail="listing unavailable")
        if req.service_type_id is not None and req.service_type_id != listing.service_type_id:
            raise HTTPException(status_code=422, detail="listing_id and service_type_id disagree")
        service_type_id = listing.service_type_id
    elif req.service_type_id is not None:
        service_type_id = req.service_type_id
    else:
        raise HTTPException(status_code=422, detail="listing_id or service_type_id required")
    cfg = repo.load_pricing_config(session, service_type_id)
```

`_listing_bookable(session, listing) -> bool` — one helper shared with the catalog's visibility semantics (active, not hidden, seller ACTIVE, payments_ready); Task 2 built the select version — extract/reuse so the two can't drift.

Floor probe: when pinned, `payouts = [seller_payout_for(cand, cfg, supply, demand)]` for `repo.candidate_for_seller(session, listing.seller_id, service_type_id)` (409 "listing unavailable" if None); unpinned path identical to today. The bump loop itself is UNCHANGED — only `probe` differs. Persist `listing_id=req.listing_id, pinned_seller_id=(listing.seller_id if listing else None)` on the Quote.

- [ ] **Step 5: Implement — create_job pinned branch + pin guard**

In `create_job`, after the quote is consumed (deleted) and the Job row is built, full code for the pinned fork:

```python
    if quote.pinned_seller_id is not None:
        listing = session.get(Listing, quote.listing_id) if quote.listing_id else None
        has_availability = (
            session.scalar(
                select(Availability).where(
                    Availability.seller_id == quote.pinned_seller_id,
                    Availability.service_type_id == job.service_type_id,
                )
            )
            is not None
        )
        if listing is None or not _listing_bookable(session, listing) or not has_availability:
            raise HTTPException(status_code=410, detail="listing no longer available")
        job.pinned_seller_id = quote.pinned_seller_id
        payout_candidate = repo.candidate_for_seller(
            session, quote.pinned_seller_id, job.service_type_id
        )
        if payout_candidate is None:
            raise HTTPException(status_code=410, detail="listing no longer available")
        supply = repo.available_count(session, job.service_type_id)
        demand = repo.active_demand(session, job.service_type_id)
        _create_offer(
            session, job,
            quote.pinned_seller_id,
            seller_payout_for(payout_candidate, cfg, supply, demand),
        )
    else:
        _match_and_offer(session, job)
```

(Adapt to the handler's real local names — it already loads `cfg`/supply/demand for dispatch; reuse rather than recompute where they exist. The 410 path happens BEFORE any charge: charges occur at accept, not job creation — assert that stays true.)

`_match_and_offer` top:

```python
    if job.pinned_seller_id is not None:
        # Browse contract: the buyer chose this seller — never substitute.
        # Pinned jobs get their offer at creation; a re-match call means
        # decline/expiry, so the job ends here.
        _expire_unmatched(session, job)
        return
```

- [ ] **Step 6: Gates** (same block; full suites both backends — watch for dispatch tests breaking, which would mean the pin leaked into the default path) **+ commit** (`src/marketplace/models.py src/marketplace/api.py src/marketplace/repo.py src/marketplace/settings.py tests/test_pinned_booking.py`, message "Add chosen-seller booking: pinned quotes and offers, human-paced TTLs")

---

### Task 4: Seed listings, demo act 9, fork docs, full gates

**Files:**
- Modify: `scripts/seed.py`, `scripts/demo.py`, `CLAUDE.md`, `README.md`, `ROADMAP.md`

**Steps:**

- [ ] **Step 1:** `seed.py` — after availability posting, the demo seller creates one listing per service type (e.g. rideshare: "City rides, vetted drivers"; cleaning: "Two-person deep clean crew"; `photo_urls=["https://placehold.co/600x400"]`); idempotent (skip if a listing with that title exists in `GET /v1/seller/listings`). Print a catalog hint line ("browse: GET /v1/catalog/listings").
- [ ] **Step 2:** `demo.py` act 9 (steps 23-24, before the final print; adapt numbering to the file): carol creates a listing → UNAUTHENTICATED `GET /v1/catalog/listings` shows it with a `price_from` → alice quotes with `listing_id` (assert `pinned_seller_id` in the quote) → job → assert carol (and only carol) got the offer → accept → complete → carol deactivates the listing → catalog no longer shows it. Extend the final summary line with "browse catalog + pinned booking live". Run before (22 steps) and after (24 steps), both exit 0; quote both.
- [ ] **Step 3:** Docs:
  - `CLAUDE.md`: retitle first heading to `# market-platform — browse marketplace on the platform-margin template`; add a fork-identity paragraph under it (private fork of the public template, upstream remote, browse-family with few big sellers, vertical TBD); add non-negotiables: listings never carry prices (platform pricing is the differentiator); public catalog responses never carry payouts/capacity/emails/payments_ready; `Listing.hidden` is admin-owned, `Listing.active` seller-owned; pinned jobs never re-match to another seller; migration count 10→11 wherever stated.
  - `README.md`: title/intro line gains the fork framing; new "Browse model" section (listings, catalog endpoints, pinned booking, TTL defaults 15/120); endpoint lists gain the seller/admin/catalog listing routes; note the template's dispatch mode remains underneath.
  - `ROADMAP.md`: new top section "Fork: market-platform" recording phase 1 (browse core — done), phase 2 (HTMX pages — next), phase 3 (transact pages), and that upstream template sections below are historical.
- [ ] **Step 4:** Full gates, bare exit codes: ruff check + format, pyright, full SQLite suite, full fork-PG suite (`DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/market_platform`), demo. Migration-chain-from-scratch check WITHOUT touching shared volumes: `docker exec marketplace-db-1 psql -U marketplace -c "CREATE DATABASE mp_freshcheck;"` → `DATABASE_URL=...5432/mp_freshcheck uv run alembic upgrade head` (expect exactly 11) → `docker exec marketplace-db-1 psql -U marketplace -c "DROP DATABASE mp_freshcheck;"`. **NEVER `docker compose down -v`.**
- [ ] **Step 5:** Commit (`scripts/seed.py scripts/demo.py CLAUDE.md README.md ROADMAP.md`, message "Document browse core: demo act 9, fork identity")

---

## Self-review notes (already applied)

- Spec coverage: §1-3 → Task 1; §4 → Task 2; §5-6 → Task 3; §7 → Task 4. Non-goals untouched (no uploads, no instant-book, no search, no pricing change).
- The quote-deleted-at-job-creation trap is handled by `Job.pinned_seller_id` (spec amendment) and the `_match_and_offer` guard covers BOTH re-match callers with one branch.
- `_listing_bookable` is deliberately shared between catalog visibility (Task 2) and booking guards (Task 3) so the two semantics cannot drift — Task 2 must build it extractable.
- `candidate_for_seller` is availability-INDEPENDENT by design (floor probe at quote time) while job time checks availability explicitly — the asymmetry is intentional and documented in the code comments.
- Type consistency: `listing_id`/`pinned_seller_id` names identical across entities/models/handlers/tests; `CatalogListingOut.price_from` matches the helper's Decimal; test file names referenced in Task 4's demo asserts don't exist (demo asserts directly).
- Line anchors verified 2026-07-16 against fork main (6fadecb); match on snippets if drifted.
