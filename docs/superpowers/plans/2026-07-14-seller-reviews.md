# Seller→Buyer Reviews (+ carried minors) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sellers rate buyers after a completed job (mirror of the existing buyer→seller review, display-only aggregate), plus the four minors carried from the disputes branch.

**Architecture:** New `SellerReview` entity mirroring `Review`, aggregate counters on `BuyerProfile`, one seller POST endpoint with the same guard ladder as `review_job`, two read surfaces (`GET /v1/profile`, `GET /v1/admin/buyers`). Migration #6 also adds a CHECK on `adjustments.amount`. Riders: dispute-creation race → 409, fake-provider seam docs, dead charge-fallback deletion.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, pytest. `uv run` everything.

**Spec:** `docs/superpowers/specs/2026-07-14-seller-reviews-design.md` — read it first.

## Global Constraints

- Python via `uv` only: `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run alembic ...`. Never pip/venv.
- **Gate on bare exit codes.** NEVER pipe a checker (`pytest | tail`, `pyright | tail`) — the pipe masks the exit code. Run the command bare; the exit code is the gate.
- Tests default to SQLite (conftest sets a temp `DATABASE_URL`). Postgres run: `docker compose up -d db` then `DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest`. If plain `docker` is denied, use `sg docker -c "..."`.
- Type hints on all public functions; pyright is strict and must stay at 0 errors.
- No `print()` in src/ — logging module (scripts/demo.py prints by design).
- A PostToolUse formatter may strip imports that are not yet used — add the import and its first use in the same edit, or re-add after.
- Money is `Decimal`, serialized as JSON strings. Ratings are plain ints.
- Commit after each green step; concise imperative messages; end body with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: SellerReview entity, BuyerProfile counters, adjustments CHECK, migration #6

**Files:**
- Modify: `src/marketplace/entities.py` (Adjustment `__table_args__`; new `SellerReview` after `Review` at ~line 218; `BuyerProfile` at line 135)
- Create: `migrations/versions/<autogen>_seller_reviews.py`
- Test: `tests/test_seller_reviews.py` (new)

**Interfaces:**
- Consumes: `Base`, `_MONEY`, `_TS`, `_now` helpers already in `entities.py`; existing `Review` as the shape to mirror.
- Produces: `SellerReview(job_id: UUID unique, seller_id: str, buyer_id: str indexed, rating: int, comment: str | None, created_at)`; `BuyerProfile.rating_count: int`, `BuyerProfile.rating_sum: int`, `BuyerProfile.rating -> float | None` (property). Task 2 imports `SellerReview`; Task 3 reads `BuyerProfile.rating`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_seller_reviews.py`:

```python
"""Seller→buyer reviews: mirror of the buyer→seller review, display-only aggregate."""

import pytest
from sqlalchemy.exc import IntegrityError

from marketplace.db import SessionLocal


def test_seller_reviews_table_registered() -> None:
    from marketplace.entities import Base

    assert "seller_reviews" in Base.metadata.tables


def test_buyer_profile_rating_property() -> None:
    from marketplace.entities import BuyerProfile

    prof = BuyerProfile(id="b1")
    assert prof.rating is None
    prof.rating_count = 2
    prof.rating_sum = 7
    assert prof.rating == 3.5


def test_adjustments_amount_check_rejects_negative() -> None:
    """DB-level backstop for the ledger doctrine: amounts are positive, kind
    carries the sign. Enforced by CHECK on both backends."""
    from decimal import Decimal
    from uuid import uuid4

    from marketplace.entities import Adjustment, Dispute, Job
    from marketplace.models import AdjustmentKind, DisputeSource

    with SessionLocal() as s:
        job = Job(
            quote_id=uuid4(),
            service_type_id="svc",
            buyer_id="b1",
            buyer_price=Decimal("10.00"),
        )
        s.add(job)
        s.flush()
        dispute = Dispute(job_id=job.id, source=DisputeSource.BUYER, buyer_id="b1", reason="x")
        s.add(dispute)
        s.flush()
        s.add(
            Adjustment(
                job_id=job.id,
                dispute_id=dispute.id,
                kind=AdjustmentKind.REFUND,
                amount=Decimal("-1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
```

(`Job`'s only no-default required fields are `quote_id`, `buyer_id`, `service_type_id`, `buyer_price` — verified against `entities.py:163-179`; `status` defaults to PENDING. Move the function-local imports to the top-level import block — the formatter/ruff will want them there.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: FAIL — `seller_reviews` not in tables; `BuyerProfile` has no `rating`; negative amount is accepted (no IntegrityError raised).

- [ ] **Step 3: Implement the entities**

In `src/marketplace/entities.py`:

(1) Add `CheckConstraint` to the existing `sqlalchemy` import line (it already imports `ForeignKey`, `String`, etc.).

(2) `Adjustment` — add table args directly under `__tablename__`:

```python
    __tablename__ = "adjustments"
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_adjustments_amount_nonneg"),)
```

(3) `BuyerProfile` — add counters + property (mirror `SellerProfile` lines 124-132):

```python
class BuyerProfile(Base):
    __tablename__ = "buyer_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    completed_jobs: Mapped[int] = mapped_column(default=0)
    rating_count: Mapped[int] = mapped_column(default=0)
    rating_sum: Mapped[int] = mapped_column(default=0)

    @property
    def rating(self) -> float | None:
        return (self.rating_sum / self.rating_count) if self.rating_count else None
```

(4) New entity directly after `Review` (~line 218), a field-for-field mirror with the id roles swapped (`buyer_id` is the indexed subject):

```python
class SellerReview(Base):
    """Seller→buyer review. Mirror of `Review`; the buyer aggregate it feeds
    is display-only — it gates nothing (see the 2026-07-14 design)."""

    __tablename__ = "seller_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    seller_id: Mapped[str] = mapped_column(String(128))
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    rating: Mapped[int] = mapped_column()
    comment: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: 3 PASS. (conftest builds the schema via `metadata.create_all`, so the new table and CHECK are live immediately.)

- [ ] **Step 5: Generate migration #6 and add the CHECK by hand**

Postgres must be up: `docker compose up -d db` (or `sg docker -c "docker compose up -d db"`).

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic revision --autogenerate -m "seller reviews"
```

Autogenerate will emit `seller_reviews` + the two `buyer_profiles` columns but **NOT** the CHECK constraint (Alembic autogenerate does not detect CHECKs). Edit the new migration file:

In `upgrade()`, after the autogenerated commands, add (batch mode so it also works on SQLite, pass-through on Postgres):

```python
    with op.batch_alter_table("adjustments") as batch_op:
        batch_op.create_check_constraint("ck_adjustments_amount_nonneg", "amount >= 0")
```

In `downgrade()`, before the autogenerated drops, add:

```python
    with op.batch_alter_table("adjustments") as batch_op:
        batch_op.drop_constraint("ck_adjustments_amount_nonneg", type_="check")
```

Also verify the autogenerated `buyer_profiles` columns carry `server_default="0"` or are added as nullable-then-backfilled — autogenerate typically emits `nullable=False` with no default, which fails on non-empty tables. Make each: `sa.Column("rating_count", sa.Integer(), nullable=False, server_default="0")` (same for `rating_sum`), matching how prior migrations handled added counter columns if any exist (check `f10e73f70fe7_auth.py` for precedent).

- [ ] **Step 6: Verify migrations from scratch (the gate is 6)**

```bash
docker compose down -v db && docker compose up -d db
sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic downgrade -1
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
```

Expected: all three exit 0; `uv run alembic history` shows 6 revisions.

- [ ] **Step 7: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0, no test regressions.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/entities.py migrations/versions tests/test_seller_reviews.py
git commit -m "Add SellerReview entity, buyer rating counters, adjustments CHECK (migration #6)"
```

---

### Task 2: POST /v1/seller/jobs/{job_id}/review

**Files:**
- Modify: `src/marketplace/models.py` (new `SellerReviewOut` after `ReviewOut` at line 209)
- Modify: `src/marketplace/api.py` (new endpoint in the seller router section, after `get_dispute_seller` ~line 685; extend the `from .entities import ...` and `from .models import ...` blocks)
- Test: `tests/test_seller_reviews.py`

**Interfaces:**
- Consumes: `SellerReview` from Task 1; existing `ReviewRequest` (reused verbatim — same fields, rating `ge=1 le=5`, comment ≤2000); `repo.get_or_create_buyer(session, buyer_id) -> BuyerProfile`; `SessionDep`, `SellerId` deps; `JobStatus`.
- Produces: `POST /v1/seller/jobs/{job_id}/review` → 200 `SellerReviewOut {id, job_id, buyer_id, rating, comment, created_at}`; 404 unknown/unowned job; 409 not-completed; 409 duplicate (including the concurrent-duplicate race). Task 3 relies on the counters this endpoint bumps; Task 5's demo calls this route.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seller_reviews.py`. Reuse the exact style of `tests/test_disputes.py`: helpers imported from `tests/test_payments` (`onboard_and_avail`, `new_job`, `accept_first_offer`), fixtures `client`, `basic_service`, `auth` (and `admin` where needed) — see `tests/conftest.py:125,144`.

```python
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient

from marketplace import api
from tests.conftest import IS_POSTGRES, AuthFactory
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def _completed_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str = "alice") -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, buyer)
    accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200
    return str(job["id"])


def test_seller_reviews_buyer_happy_path_and_aggregate(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    r = client.post(
        f"/v1/seller/jobs/{job_id}/review",
        json={"rating": 4, "comment": "prompt payment"},
        headers=auth("seller", "s1"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buyer_id"] == "alice"
    assert body["rating"] == 4
    assert "seller_id" not in body  # mirror of ReviewOut: author id not echoed

    # Second job, second review: aggregate is the running mean.
    job2 = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job2['id']}/complete", headers=auth("seller", "s1"))
    r = client.post(
        f"/v1/seller/jobs/{job2['id']}/review", json={"rating": 1}, headers=auth("seller", "s1")
    )
    assert r.status_code == 200

    from marketplace.entities import BuyerProfile

    with SessionLocal() as s:
        prof = s.get(BuyerProfile, "alice")
        assert prof is not None
        assert prof.rating_count == 2
        assert prof.rating_sum == 5
        assert prof.rating == 2.5


def test_review_unknown_job_404(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    r = client.post(
        f"/v1/seller/jobs/{uuid4()}/review", json={"rating": 5}, headers=auth("seller", "s1")
    )
    assert r.status_code == 404


def test_review_not_own_job_404(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    r = client.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=auth("seller", "other")
    )
    assert r.status_code == 404


def test_review_incomplete_job_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))  # ACCEPTED, not COMPLETED
    r = client.post(
        f"/v1/seller/jobs/{job['id']}/review", json={"rating": 5}, headers=auth("seller", "s1")
    )
    assert r.status_code == 409


def test_review_duplicate_409(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    assert (
        client.post(
            f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=seller
        ).status_code
        == 200
    )
    r = client.post(f"/v1/seller/jobs/{job_id}/review", json={"rating": 1}, headers=seller)
    assert r.status_code == 409


def test_review_schema_bounds(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    for bad in ({"rating": 0}, {"rating": 6}, {"rating": 3, "comment": "x" * 2001}):
        r = client.post(f"/v1/seller/jobs/{job_id}/review", json=bad, headers=seller)
        assert r.status_code == 422, bad


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_review_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """Two threads race the same review; UNIQUE(job_id) is the backstop and the
    loser must get the sequential path's 409, not a 500."""
    job_id = _completed_job(client, auth, basic_service)
    seller = auth("seller", "s1")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(
            f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=seller
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [200, 409], codes

    from marketplace.entities import SellerReview
    from sqlalchemy import select

    with SessionLocal() as s:
        assert len(s.scalars(select(SellerReview)).all()) == 1
```

Consolidate imports at the top of the file (single import block — the formatter will complain otherwise).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: the new tests FAIL with 404s (route does not exist → FastAPI 404 on unknown path, assertions on 200/409 fail). The PG test SKIPs on SQLite.

- [ ] **Step 3: Add SellerReviewOut to models.py**

After `ReviewOut` (line 209):

```python
class SellerReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    buyer_id: str
    rating: int
    comment: str | None
    created_at: datetime
```

- [ ] **Step 4: Add the endpoint to api.py**

Extend the existing import blocks: `SellerReview` in the `from .entities import` block, `SellerReviewOut` in the `from .models import` block, and add `IntegrityError` — check whether `sqlalchemy.exc` is already imported; if not, add `from sqlalchemy.exc import IntegrityError`.

Place after `get_dispute_seller` (~line 685), mirroring `review_job` (`api.py:457`) guard-for-guard:

```python
@seller_router.post("/jobs/{job_id}/review", response_model=SellerReviewOut)
def review_buyer(
    job_id: UUID, body: ReviewRequest, session: SessionDep, seller_id: SellerId
) -> SellerReview:
    job = session.get(Job, job_id)
    if job is None or job.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="can only review a completed job")
    if session.scalar(select(SellerReview).where(SellerReview.job_id == job_id)) is not None:
        raise HTTPException(status_code=409, detail="job already reviewed")

    review = SellerReview(
        job_id=job.id,
        seller_id=seller_id,
        buyer_id=job.buyer_id,
        rating=body.rating,
        comment=body.comment,
    )
    session.add(review)
    buyer = repo.get_or_create_buyer(session, job.buyer_id)
    buyer.rating_count += 1
    buyer.rating_sum += body.rating
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise HTTPException(status_code=409, detail="job already reviewed") from None
    return review
```

Note: the body reuses the existing `ReviewRequest` — identical fields, DRY. The response schema is what differs (`buyer_id` in place of `seller_id`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: all PASS (PG race test skipped on SQLite).

- [ ] **Step 6: Run the PG-gated race test on Postgres**

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_seller_reviews.py -v
```

Expected: all PASS including `test_concurrent_duplicate_review_races_to_409`, exit 0.

- [ ] **Step 7: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py tests/test_seller_reviews.py
git commit -m "Add seller-to-buyer review endpoint with race-safe duplicate guard"
```

---

### Task 3: Display surfaces — GET /v1/profile and GET /v1/admin/buyers

**Files:**
- Modify: `src/marketplace/models.py` (new `BuyerProfileOut` after `SellerProfileOut` at ~line 275)
- Modify: `src/marketplace/api.py` (buyer endpoint near the other buyer routes ~line 455; admin endpoint after `admin_update_seller` ~line 983)
- Test: `tests/test_seller_reviews.py`

**Interfaces:**
- Consumes: `BuyerProfile` (Task 1 counters + `rating` property); `repo.get_or_create_buyer`; `BuyerId`, `AdminId` deps.
- Produces: `GET /v1/profile` → `BuyerProfileOut {id, rating, rating_count, completed_jobs}` (buyer's own); `GET /v1/admin/buyers` → `list[BuyerProfileOut]` ordered by id. Task 5's demo calls `GET /v1/profile`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seller_reviews.py`:

```python
def test_buyer_profile_surface(client: TestClient, basic_service: str, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    r = client.get("/v1/profile", headers=buyer)
    assert r.status_code == 200
    assert r.json() == {"id": "alice", "rating": None, "rating_count": 0, "completed_jobs": 0}

    job_id = _completed_job(client, auth, basic_service)
    client.post(f"/v1/seller/jobs/{job_id}/review", json={"rating": 4}, headers=auth("seller", "s1"))
    body = client.get("/v1/profile", headers=buyer).json()
    assert body["rating"] == 4.0
    assert body["rating_count"] == 1
    assert body["completed_jobs"] == 1


def test_admin_buyers_list(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    client.post(f"/v1/seller/jobs/{job_id}/review", json={"rating": 5}, headers=auth("seller", "s1"))
    r = client.get("/v1/admin/buyers", headers=admin)
    assert r.status_code == 200
    rows = {b["id"]: b for b in r.json()}
    assert rows["alice"]["rating"] == 5.0
    # Role guard: a buyer token cannot read the admin list.
    assert client.get("/v1/admin/buyers", headers=auth("buyer", "alice")).status_code == 403
```

(`Header` is already imported from `tests.conftest` in this file's import block — add it if Task 2 didn't.)

NOTE: verify the role-guard status code — check what existing tests assert for non-admin access to admin routes (grep `admin` guard tests in `tests/test_auth_and_hardening.py`); use that exact code (403 or 401) in the assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: the two new tests FAIL (routes don't exist).

- [ ] **Step 3: Add BuyerProfileOut to models.py**

After `SellerProfileOut`:

```python
class BuyerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rating: float | None
    rating_count: int
    completed_jobs: int
```

- [ ] **Step 4: Add the two endpoints to api.py**

Import `BuyerProfileOut` in the models import block and `BuyerProfile` in the entities import block (if not already imported).

Buyer route, placed with the other buyer endpoints (before `review_job` is fine). The buyer router prefix is `/v1`, so this is `GET /v1/profile`, mirroring `GET /v1/seller/profile` (`api.py:573`):

```python
@buyer_router.get("/profile", response_model=BuyerProfileOut)
def get_buyer_profile(session: SessionDep, buyer_id: BuyerId) -> BuyerProfile:
    return repo.get_or_create_buyer(session, buyer_id)
```

Admin route, placed after `admin_update_seller`:

```python
@admin_router.get("/buyers", response_model=list[BuyerProfileOut])
def admin_list_buyers(session: SessionDep, admin_id: AdminId) -> Sequence[BuyerProfile]:
    return session.scalars(select(BuyerProfile).order_by(BuyerProfile.id)).all()
```

NOTE: check how other admin list endpoints type their return (`list_transactions` etc.) and match — if they return `Sequence[...]` ensure `Sequence` is imported from `collections.abc`; if they convert with `list(...)`, do the same.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_seller_reviews.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py tests/test_seller_reviews.py
git commit -m "Surface buyer aggregate: GET /v1/profile and GET /v1/admin/buyers"
```

---

### Task 4: Carried minors — dispute race 409, seam docs, dead charge fallback

**Files:**
- Modify: `src/marketplace/api.py` (`open_dispute` ~line 483; also `review_job` ~line 457 gets the same two-line race guard)
- Modify: `src/marketplace/payments/fake.py` (class docstring + seam comments only)
- Modify: `src/marketplace/payments/stripe_provider.py` (line ~206)
- Test: `tests/test_disputes.py` (race test), `tests/test_stripe_provider.py` or wherever `parse_webhook` unit tests live (grep `charge.dispute` under tests/)

**Interfaces:**
- Consumes: existing `open_dispute`, `review_job`, `FakeProvider`, `StripeProvider.parse_webhook`.
- Produces: no new interfaces — behavior fixes and docs. Concurrent duplicate dispute → 409; `related_id` is PaymentIntent-only.

- [ ] **Step 1: Write the failing race test for open_dispute**

Append to `tests/test_disputes.py` (it already imports `IS_POSTGRES`? — if not, extend the `from tests.conftest import` line; also `threading`, `ThreadPoolExecutor`, `api` as needed):

```python
@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_dispute_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """UNIQUE(disputes.job_id) backstops the duplicate check; the loser must
    get the sequential path's 409, not a 500."""
    job_id = _completed_job(client, auth, basic_service)
    buyer = auth("buyer", "alice")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.post(
            f"/v1/jobs/{job_id}/dispute", json={"reason": "raced"}, headers=buyer
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [201, 409], codes

    with SessionLocal() as s:
        assert len(s.scalars(select(Dispute).where(Dispute.job_id == UUID(job_id))).all()) == 1
```

- [ ] **Step 2: Run it on Postgres to verify it fails with a 500**

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_disputes.py::test_concurrent_duplicate_dispute_races_to_409 -v
```

Expected: FAIL — codes are `[201, 500]`.

- [ ] **Step 3: Guard the flush in open_dispute (and review_job — same defect class)**

In `open_dispute` (`api.py:~499`), the current `session.flush()` after `session.add(dispute)` becomes:

```python
    try:
        session.flush()
    except IntegrityError:
        # Concurrent duplicate lost the UNIQUE(job_id) race — same answer as
        # the sequential duplicate, not a 500.
        raise HTTPException(status_code=409, detail="job already disputed") from None
```

In `review_job` (`api.py:~475`), the trailing `session.flush()` gets the identical guard with detail `"job already reviewed"`. (No dedicated race test — same two-line shape as `open_dispute` and Task 2's `review_buyer`, both race-tested.)

- [ ] **Step 4: Verify the race test passes on Postgres, then the full SQLite suite**

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_disputes.py -v
uv run pytest
```

Expected: both exit 0.

- [ ] **Step 5: Declare the fake-provider seam asymmetry (docs only)**

In `src/marketplace/payments/fake.py`, extend the `FakeProvider` class docstring with:

```
    Recording seams — two deliberate semantics:
      * ATTEMPT-recorded: `transfer_keys` appends before the failure checks, so
        tests can prove WHICH idempotency key a failed attempt used (the
        payout-retry tests rely on this to show original-key replay).
      * SUCCESS-recorded: `refunded`, `refund_keys`, `refund_amounts`,
        `reversals`, `cancelled` append only after the failure checks, so
        counts equal executed provider legs (the dispute-orphan tests rely on
        this). Do not "align" one to the other; both directions are load-bearing.
```

Adjust the inline comment on `transfer_keys.append` (fake.py:115) to point at the docstring: `# ATTEMPT-recorded — see class docstring`. No behavior change; no new test.

- [ ] **Step 6: Delete the dead charge-only related_id fallback (TDD)**

The `parse_webhook` unit tests live in `tests/test_stripe_provider.py`, which already has `_event(type, obj)` and `_signed(payload)` helpers plus a `provider` fixture (see `test_chargeback_events_map_and_carry_fields` at line 73). Add:

```python
def test_dispute_event_without_payment_intent_has_no_related_id(provider: StripeProvider) -> None:
    """related_id is PaymentIntent-only: the consumer matches it against
    Payment.provider_payment_id (always a PI id), so a bare charge id could
    never match — carrying it just manufactured a misleading 'unknown charge'
    lookup."""
    payload = _event("charge.dispute.created", {"id": "dp_2", "charge": "ch_1", "amount": 500})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "chargeback_opened"
    assert event.related_id is None
    assert event.amount_minor == 500
```

Run: `uv run pytest tests/test_stripe_provider.py::test_dispute_event_without_payment_intent_has_no_related_id -v`
Expected: FAIL — `related_id == "ch_1"`.

Then in `src/marketplace/payments/stripe_provider.py` line ~206 change:

```python
            related_id = str(obj.get("payment_intent") or obj.get("charge") or "") or None
```

to:

```python
            # PaymentIntent-only: the consumer matches Payment.provider_payment_id,
            # which is always a PI id — a charge id could never match.
            related_id = str(obj.get("payment_intent") or "") or None
```

If an existing test asserted the charge fallback, repoint it to assert `related_id is None` for charge-only events. Re-run the file: expected PASS.

- [ ] **Step 7: Lint, typecheck, both suites — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/api.py src/marketplace/payments/fake.py src/marketplace/payments/stripe_provider.py tests/
git commit -m "Close carried disputes minors: race-safe dispute creation, seam semantics declared, PI-only dispute related_id"
```

---

### Task 5: Demo act, docs, full gates

**Files:**
- Modify: `scripts/demo.py` (Act 1, after step 7 at ~line 128)
- Modify: `ROADMAP.md` (move seller reviews into Done; shrink the T&S "remaining" list)
- Modify: `README.md` (add the three new endpoints wherever existing endpoints are listed — check the file's structure first; if it has no endpoint table, skip)
- Modify: `SECURITY.md` (one bullet in the asymmetric-views section: buyer sees only their aggregate, never individual seller reviews/comments)
- Modify: `CLAUDE.md` (only if it enumerates endpoints/tables — check first)

**Interfaces:**
- Consumes: `POST /v1/seller/jobs/{job_id}/review` (Task 2), `GET /v1/profile` (Task 3).
- Produces: demo proves the loop end-to-end; docs current.

- [ ] **Step 1: Add the demo act**

In `scripts/demo.py` after step 7 ("Alice reviews Carol", ~line 128), insert:

```python
    print("7b. Carol reviews Alice back — buyer aggregate is display-only")
    c.post(f"/v1/seller/jobs/{job_id}/review", json={"rating": 5, "comment": "prompt"}, headers=carol)
    profile = c.get("/v1/profile", headers=alice).json()
    print(f"   alice rating = {profile['rating']} ({profile['rating_count']} review)")
    assert profile["rating"] == 5.0
```

- [ ] **Step 2: Run the demo**

Run: `uv run python scripts/demo.py`
Expected: exit 0, the new lines print between steps 7 and 8.

- [ ] **Step 3: Update the docs**

- `ROADMAP.md`: in the T&S item (line ~82), mark seller→buyer reviews done (fold a two-line summary into the "Done" section mirroring the disputes entry style: mirror table, display-only aggregate, `/v1/profile` + `/v1/admin/buyers`, carried minors closed); remaining sub-phases become "moderation/abuse → notification preferences". Also delete the four carried-minor mentions if ROADMAP lists them.
- `SECURITY.md`: one bullet where dispute view-asymmetry is documented: seller→buyer reviews expose only the aggregate to the buyer; individual reviews/comments are admin-side (moderation phase decides more).
- `README.md` / `CLAUDE.md`: check each for an endpoint or schema enumeration; update only what exists, mirroring current formatting.

- [ ] **Step 4: Full gates — bare exit codes, both backends, migrations from scratch**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
uv run python scripts/demo.py
```

Then the fresh-DB migration gate (destroys the compose db volume):

```bash
docker compose down -v db && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
```

Expected: every command exits 0; SQLite suite has no new failures (2 pre-existing skips remain), Postgres suite 0 skips of the non-PG-gated kind (the PG run executes everything).

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py ROADMAP.md README.md SECURITY.md CLAUDE.md
git commit -m "Document seller-to-buyer reviews: demo act 7b, roadmap, security notes"
```

---

## Self-review notes (already applied)

- Spec coverage: entity/counters/CHECK → Task 1; endpoint + race guard → Task 2; surfaces → Task 3; riders (a)(b)(c) → Task 4 ((d) rode Task 1); demo/docs/gates → Task 5. Non-goals untouched.
- Rider (b) was amended at planning time (spec updated in the same commit): both seam directions are test-guarded, so it is documentation, not a behavior change.
- The buyer surface is `GET /v1/profile` (buyer router prefix is `/v1`), amended from the spec's original `/v1/buyer/me`.
- Type consistency: `SellerReview` fields match between Tasks 1/2; `BuyerProfileOut` fields match `BuyerProfile` (Task 1) + `from_attributes` picks up the `rating` property.
