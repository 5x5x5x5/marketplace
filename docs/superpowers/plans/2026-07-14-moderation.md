# Moderation & Abuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin enforcement (user suspension, content takedown) + a counterparty-only reporting pipeline, with no automation between them.

**Architecture:** State columns on existing entities (`User.status`, `comment_hidden` on both review tables) + one `reports` table shaped like `disputes`. Enforcement is an explicit `_require_active` guard at each acquisition endpoint (verb-gating: login, completion, and exit verbs stay open). Matching excludes suspended sellers via an anti-join. Everything admin-initiated goes through the existing `repo.audit`. Migration #7.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, pytest. `uv run` everything.

**Spec:** `docs/superpowers/specs/2026-07-14-moderation-design.md` — read it first.

## Global Constraints

- Python via `uv` only: `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run alembic ...`. Never pip/venv.
- **Gate on bare exit codes.** NEVER pipe a checker (`pytest | tail`, `pyright | tail`) — run the command bare; the exit code is the gate.
- Tests default to SQLite (conftest sets a temp `DATABASE_URL`). Postgres: `docker compose up -d db` then `DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest`. If plain `docker` is denied, wrap as `sg docker -c "..."`.
- Pyright is strict; must end at 0 errors. Type hints on all public functions. No `print()` in src/ (scripts/demo.py prints by design).
- A PostToolUse formatter may strip imports that are not yet used — add an import and its first use in the same edit, or re-add after.
- **Ordering lesson (from the seller-reviews branch):** in any endpoint that both queries and inserts, do ALL reads (including `get_or_create_*`) BEFORE `session.add(...)`; the guarded `session.flush()` must be the only statement that can hit a UNIQUE violation — autoflush inside a later query would raise OUTSIDE the try/except guard.
- Resolving a report never auto-suspends or auto-hides. Nothing is ever deleted. Every admin moderation action calls `audit(session, admin_id, <action>, <target>, <detail>)` (imported from repo, already in api.py's namespace).
- Suspension gates ONLY these acquisition verbs: buyer create_quote/create_job/review_job/open_dispute; seller availability/accept_offer/onboard_payments/review_buyer; both file_report. Login, logout, all GETs, buyer cancel, seller decline, seller complete, webhooks stay open to suspended users.
- Commit style: concise, imperative; end body with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No backticks inside double-quoted `git commit -m` strings.

---

### Task 1: Enums, entity columns, Report entity, migration #7

**Files:**
- Modify: `src/marketplace/models.py` (enums section ~line 48-113)
- Modify: `src/marketplace/entities.py` (`User` ~line 318, `Review` ~line 216, `SellerReview` ~line 228, new `Report` after `SellerReview`)
- Create: `migrations/versions/<autogen>_moderation.py`
- Test: `tests/test_moderation.py` (new)

**Interfaces:**
- Consumes: existing `_enum`, `_TS`, `_now`, `String`, `UniqueConstraint` helpers in entities.py; `StrEnum` pattern in models.py.
- Produces (later tasks import these): `UserStatus.ACTIVE/SUSPENDED`; `ReportTargetKind.USER/REVIEW/SELLER_REVIEW`; `ReportStatus.OPEN/ACTIONED/DISMISSED`; `EventKind.REPORT_OPENED_ADMIN`; `User.status/suspended_reason/suspended_at`; `Review.comment_hidden`, `SellerReview.comment_hidden`, property `public_comment -> str | None` on both; entity `Report(reporter_id: str, target_kind, target_id: str, reason: str, status, resolution_note, created_at, resolved_at)` with `UNIQUE(reporter_id, target_kind, target_id)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_moderation.py`:

```python
"""Moderation: suspension, content takedown, reports. Spec: 2026-07-14-moderation-design.md."""


def test_moderation_schema_registered() -> None:
    from marketplace.entities import Base

    assert "reports" in Base.metadata.tables
    users = Base.metadata.tables["users"]
    assert "status" in users.c and "suspended_reason" in users.c and "suspended_at" in users.c
    assert "comment_hidden" in Base.metadata.tables["reviews"].c
    assert "comment_hidden" in Base.metadata.tables["seller_reviews"].c


def test_public_comment_property_is_the_invariant_home() -> None:
    """Non-admin serializations read public_comment; hiding nulls it, nothing else."""
    from marketplace.entities import Review, SellerReview

    for cls in (Review, SellerReview):
        row = cls(rating=3, comment="rude text")
        assert row.public_comment == "rude text"
        row.comment_hidden = True
        assert row.public_comment is None
        assert row.comment == "rude text"  # the row itself is untouched
        row.comment_hidden = False
        assert row.public_comment == "rude text"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: FAIL — no `reports` table, no `status` column, no `comment_hidden`/`public_comment`.

- [ ] **Step 3: Add the enums to models.py**

After `DisputeStatus` (~line 106), following the existing `StrEnum` style:

```python
class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"  # verb-gated: acquisition blocked, completion/exit allowed


class ReportTargetKind(StrEnum):
    USER = "user"
    REVIEW = "review"  # buyer→seller review (reviews table)
    SELLER_REVIEW = "seller_review"  # seller→buyer review


class ReportStatus(StrEnum):
    OPEN = "open"
    ACTIONED = "actioned"  # terminal; the tools were used (or not) explicitly
    DISMISSED = "dismissed"  # terminal
```

And in `EventKind` (line 74), after `CHARGEBACK_CLOSED_ADMIN`:

```python
    REPORT_OPENED_ADMIN = "report_opened_admin"
```

- [ ] **Step 4: Add the entity changes**

In `src/marketplace/entities.py` — extend the `from .models import ...` block with `ReportStatus`, `ReportTargetKind`, `UserStatus` (keep alphabetical order).

`User` (after `email_verified`, before `created_at`):

```python
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus), default=UserStatus.ACTIVE, index=True
    )
    suspended_reason: Mapped[str | None] = mapped_column(String(2000), default=None)
    suspended_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
```

`Review` AND `SellerReview` (identical addition, after `comment`):

```python
    comment_hidden: Mapped[bool] = mapped_column(default=False)
```

and on each, after `created_at`:

```python
    @property
    def public_comment(self) -> str | None:
        """Single home of the takedown invariant: non-admin views read this."""
        return None if self.comment_hidden else self.comment
```

New entity after `SellerReview`:

```python
class Report(Base):
    """User-filed abuse report. Paper trail only: resolving one never
    auto-suspends or auto-hides — admins act with the explicit tools."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("reporter_id", "target_kind", "target_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reporter_id: Mapped[str] = mapped_column(String(128), index=True)
    target_kind: Mapped[ReportTargetKind] = mapped_column(_enum(ReportTargetKind))
    target_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(2000))
    status: Mapped[ReportStatus] = mapped_column(
        _enum(ReportStatus), default=ReportStatus.OPEN, index=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: 2 PASS.

- [ ] **Step 6: Generate migration #7 and fix defaults by hand**

Postgres must be up: `docker compose up -d db` (or `sg docker -c "..."`).

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic revision --autogenerate -m "moderation"
```

Autogenerate emits the `reports` table and the five added columns but NOT server defaults. Edit the migration so existing rows survive the NOT NULL additions:

- `users.status`: `sa.Column("status", sa.Enum("active", "suspended", name="userstatus", native_enum=False, length=32), nullable=False, server_default="active")`
- `reviews.comment_hidden` and `seller_reviews.comment_hidden`: `sa.Column("comment_hidden", sa.Boolean(), nullable=False, server_default=sa.false())`
- `suspended_reason`/`suspended_at` are nullable — no default needed.
- Keep the autogenerated index create/drop lines (`ix_users_status`, `ix_reports_reporter_id`, `ix_reports_status`) and the `reports` unique constraint.

- [ ] **Step 7: Verify migrations from scratch (the gate is 7)**

```bash
docker compose down -v db && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic downgrade -1
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic history
```

Expected: all exit 0; history shows 7 revisions.

- [ ] **Step 8: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0, no regressions.

- [ ] **Step 9: Commit**

```bash
git add src/marketplace/models.py src/marketplace/entities.py migrations/versions tests/test_moderation.py
git commit -m "Add moderation schema: user status, comment_hidden, reports (migration #7)"
```

---

### Task 2: Suspension — admin endpoints, verb guards, matching exclusion

**Files:**
- Modify: `src/marketplace/models.py` (new request/response schemas)
- Modify: `src/marketplace/api.py` (helper `_require_active`; two admin endpoints after `admin_update_seller` ~line 1025; one-line guards at 8 endpoints)
- Modify: `src/marketplace/repo.py` (`eligible_candidates` line 121)
- Test: `tests/test_moderation.py`

**Interfaces:**
- Consumes: Task 1's `UserStatus`; existing `User`, `audit`, `_now`, `SessionDep`, `AdminId`, `BuyerId`, `SellerId`.
- Produces: `_require_active(session: Session, user_id: str) -> None` (raises 403; Task 4 calls it in file_report); `POST /v1/admin/users/{user_id}/suspend` body `{reason}` → `UserModerationOut {id, display_name, status, suspended_reason, suspended_at}`; `POST /v1/admin/users/{user_id}/reinstate` → same schema (Task 3 reuses `UserModerationOut` for reset_display_name).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_moderation.py` (helpers mirror `tests/test_disputes.py`; `auth` white-box fixture creates the User row with id == sub):

```python
import pytest
from fastapi.testclient import TestClient

from marketplace.db import SessionLocal
from tests.conftest import AuthFactory, Header
from tests.test_payments import accept_first_offer, new_job, onboard_and_avail


def _suspend(client: TestClient, admin: Header, user_id: str, reason: str = "abuse") -> dict[str, object]:
    r = client.post(f"/v1/admin/users/{user_id}/suspend", json={"reason": reason}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def test_suspend_lifecycle(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("buyer", "alice")  # materialize the user row
    body = _suspend(client, admin, "alice")
    assert body["status"] == "suspended"
    assert body["suspended_reason"] == "abuse"
    assert body["suspended_at"] is not None
    # double-suspend -> 409
    assert (
        client.post("/v1/admin/users/alice/suspend", json={"reason": "x"}, headers=admin).status_code
        == 409
    )
    r = client.post("/v1/admin/users/alice/reinstate", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["suspended_reason"] is None
    # double-reinstate -> 409
    assert client.post("/v1/admin/users/alice/reinstate", headers=admin).status_code == 409
    # unknown -> 404
    assert (
        client.post("/v1/admin/users/nobody/suspend", json={"reason": "x"}, headers=admin).status_code
        == 404
    )


def test_admins_cannot_be_suspended(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("admin", "root2")
    r = client.post("/v1/admin/users/root2/suspend", json={"reason": "x"}, headers=admin)
    assert r.status_code == 422


def test_suspended_buyer_verbs(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Acquisition 403s; exit verbs and reads still work (freeze-new/finish-in-flight)."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    buyer = auth("buyer", "alice")
    _suspend(client, admin, "alice")

    r = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"
    # reads still work
    assert client.get("/v1/jobs", headers=buyer).status_code == 200
    assert client.get("/v1/profile", headers=buyer).status_code == 200
    # exit verb still works: cancel the accepted job
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=buyer).status_code == 200


def test_suspended_seller_verbs(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Seller acquisition 403s; complete (finish verb) still works."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    seller = auth("seller", "s1")
    _suspend(client, admin, "s1")

    assert (
        client.post("/v1/seller/availability", json={"service_type_id": basic_service}, headers=seller).status_code
        == 403
    )
    # finish verb allowed: complete the in-flight job
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=seller)
    assert r.status_code == 200
    # gated after completion too: reviewing the buyer is acquisition
    r = client.post(f"/v1/seller/jobs/{job['id']}/review", json={"rating": 1}, headers=seller)
    assert r.status_code == 403


def test_suspended_seller_leaves_matching(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Job.status stays "pending" either way (offers are separate rows) — the
    observable is the seller's offers list."""
    onboard_and_avail(client, auth, basic_service, "s1")
    _suspend(client, admin, "s1")
    new_job(client, auth, basic_service, "alice")
    assert client.get("/v1/seller/offers", headers=auth("seller", "s1")).json() == []

    # reinstate -> the next job creation matches again
    client.post("/v1/admin/users/s1/reinstate", headers=admin)
    new_job(client, auth, basic_service, "alice")
    assert len(client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: new tests FAIL (404 — suspend route doesn't exist).

- [ ] **Step 3: Add schemas to models.py**

After `BuyerProfileOut`:

```python
class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class UserModerationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    status: UserStatus
    suspended_reason: str | None
    suspended_at: datetime | None
```

- [ ] **Step 4: Implement guard, endpoints, matching exclusion**

api.py — extend imports: `User` in the entities block (if absent), `SuspendRequest`, `UserModerationOut`, `UserStatus` in the models block.

Helper next to the other module-level helpers (near `_now` usage, before the routers):

```python
def _require_active(session: Session, user_id: str) -> None:
    """Verb gate for suspension: acquisition endpoints call this first.
    A missing row is treated as active (auth already proved the principal)."""
    user = session.get(User, user_id)
    if user is not None and user.status is UserStatus.SUSPENDED:
        raise HTTPException(status_code=403, detail="account suspended")
```

Admin endpoints after `admin_update_seller`:

```python
@admin_router.post("/users/{user_id}/suspend", response_model=UserModerationOut)
def suspend_user(
    user_id: str, body: SuspendRequest, session: SessionDep, admin_id: AdminId
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.role is UserRole.ADMIN:
        raise HTTPException(status_code=422, detail="admins cannot be suspended")
    if user.status is UserStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="user already suspended")
    user.status = UserStatus.SUSPENDED
    user.suspended_reason = body.reason
    user.suspended_at = _now()
    audit(session, admin_id, "suspend_user", user_id, {"reason": body.reason})
    session.flush()
    return user


@admin_router.post("/users/{user_id}/reinstate", response_model=UserModerationOut)
def reinstate_user(user_id: str, session: SessionDep, admin_id: AdminId) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.status is not UserStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="user is not suspended")
    user.status = UserStatus.ACTIVE
    user.suspended_reason = None
    user.suspended_at = None
    audit(session, admin_id, "reinstate_user", user_id, {})
    session.flush()
    return user
```

(`UserRole` is already imported in api.py via auth usage — verify, add if missing.)

One-line guards, first statement in each handler body (after the docstring where one exists):

- `create_quote` (api.py:~309): `_require_active(session, buyer_id)`
- `create_job` (~365): `_require_active(session, buyer_id)`
- `review_job` (~468): `_require_active(session, buyer_id)`
- `open_dispute` (~502): `_require_active(session, buyer_id)`
- `onboard_payments` (~603): `_require_active(session, seller_id)`
- availability POST handler (~631): `_require_active(session, seller_id)`
- `review_buyer` (~710): `_require_active(session, seller_id)`
- `accept_offer` (~746): `_require_active(session, seller_id)`

Do NOT guard: cancel_job, decline_offer, complete_job, any GET, DELETE availability (removing availability is an exit verb), webhooks, auth routes.

repo.py — `eligible_candidates` (line 121): extend the entities import with `User` and the models import with `UserStatus`, then:

```python
    avails = session.scalars(
        select(Availability).where(Availability.service_type_id == service_type_id)
    ).all()
    if not avails:
        return []
    suspended = set(
        session.scalars(
            select(User.id).where(
                User.id.in_([a.seller_id for a in avails]),
                User.status == UserStatus.SUSPENDED,
            )
        ).all()
    )
    out: list[Candidate] = []
    for a in avails:
        if a.seller_id in exclude or a.seller_id in suspended:
            continue
```

(rest of the loop unchanged; a profile without a User row stays eligible by construction — it can't be in `suspended`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0 — especially no regressions in test_payments/test_auth (the guards must not fire for active users).

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py src/marketplace/repo.py tests/test_moderation.py
git commit -m "Add user suspension: admin endpoints, acquisition-verb guards, matching exclusion"
```

---

### Task 3: Content takedown — hide/unhide, admin review lists, display-name reset

**Files:**
- Modify: `src/marketplace/models.py` (`AdminReviewOut`; repoint `ReviewOut`/`SellerReviewOut` comment)
- Modify: `src/marketplace/api.py` (admin endpoints after `reinstate_user`)
- Test: `tests/test_moderation.py`

**Interfaces:**
- Consumes: Task 1's `comment_hidden`/`public_comment`; Task 2's `UserModerationOut`; existing `Review`, `SellerReview`, `audit`.
- Produces: `GET /v1/admin/reviews/{kind}` (`kind` ∈ buyer/seller) → `list[AdminReviewOut {id, job_id, author_id, subject_id, rating, comment, comment_hidden, created_at}]` newest-first; `POST /v1/admin/reviews/{kind}/{review_id}/hide` and `/unhide` → `AdminReviewOut`; `POST /v1/admin/users/{user_id}/reset_display_name` → `UserModerationOut`. Task 4's report tests may reference review ids from these lists.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_moderation.py`:

```python
def _reviewed_job(client: TestClient, basic_service: str, auth: AuthFactory) -> str:
    """Completed job where alice reviewed s1 (buyer-kind review). Returns job id."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accept_first_offer(client, auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    r = client.post(
        f"/v1/jobs/{job['id']}/review",
        json={"rating": 2, "comment": "rude and late"},
        headers=auth("buyer", "alice"),
    )
    assert r.status_code == 200, r.text
    return str(job["id"])


def test_hide_and_unhide_review(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)
    listed = client.get("/v1/admin/reviews/buyer", headers=admin).json()
    assert len(listed) == 1
    review = listed[0]
    assert review["author_id"] == "alice" and review["subject_id"] == "s1"
    assert review["comment"] == "rude and late" and review["comment_hidden"] is False

    r = client.post(f"/v1/admin/reviews/buyer/{review['id']}/hide", headers=admin)
    assert r.status_code == 200
    assert r.json()["comment_hidden"] is True
    assert r.json()["comment"] == "rude and late"  # admin still sees the text
    # idempotence guard
    assert client.post(f"/v1/admin/reviews/buyer/{review['id']}/hide", headers=admin).status_code == 409
    # aggregate untouched by hiding
    with SessionLocal() as s:
        from marketplace.entities import SellerProfile

        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.rating_count == 1 and prof.rating_sum == 2

    r = client.post(f"/v1/admin/reviews/buyer/{review['id']}/unhide", headers=admin)
    assert r.status_code == 200 and r.json()["comment_hidden"] is False
    assert client.post(f"/v1/admin/reviews/buyer/{review['id']}/unhide", headers=admin).status_code == 409


def test_hide_seller_review_kind(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _reviewed_job(client, basic_service, auth)
    client.post(
        f"/v1/seller/jobs/{job_id}/review", json={"rating": 1, "comment": "bad buyer"},
        headers=auth("seller", "s1"),
    )
    listed = client.get("/v1/admin/reviews/seller", headers=admin).json()
    assert len(listed) == 1
    assert listed[0]["author_id"] == "s1" and listed[0]["subject_id"] == "alice"
    r = client.post(f"/v1/admin/reviews/seller/{listed[0]['id']}/hide", headers=admin)
    assert r.status_code == 200
    # unknown id -> 404 (valid UUID, no row)
    from uuid import uuid4

    assert client.post(f"/v1/admin/reviews/seller/{uuid4()}/hide", headers=admin).status_code == 404


def test_reset_display_name(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    auth("buyer", "alice")
    r = client.post("/v1/admin/users/alice/reset_display_name", headers=admin)
    assert r.status_code == 200
    assert r.json()["display_name"] == "user-" + "alice"[:8]
    assert client.post("/v1/admin/users/nobody/reset_display_name", headers=admin).status_code == 404
```

(Hoist the `SellerProfile` and `uuid4` imports to the file's top-level import block — shown inline here only for reading order.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: new tests FAIL (routes don't exist).

- [ ] **Step 3: Add AdminReviewOut and repoint the public comment**

models.py, after `SellerReviewOut`:

```python
class AdminReviewOut(BaseModel):
    """Unified admin view over both review tables; party ids normalized to
    author/subject. Admin sees the raw comment plus the hidden flag."""

    id: UUID
    job_id: UUID
    author_id: str
    subject_id: str
    rating: int
    comment: str | None
    comment_hidden: bool
    created_at: datetime
```

In BOTH `ReviewOut` and `SellerReviewOut`, change the comment field to read the invariant property (serialization name stays `comment`; creation responses are unaffected because a fresh row is never hidden):

```python
    comment: str | None = Field(default=None, validation_alias="public_comment")
```

- [ ] **Step 4: Implement the endpoints**

api.py — extend the models import with `AdminReviewOut`; add `from typing import Literal` if not present (check the typing import line). After `reinstate_user`:

```python
def _admin_review_out(row: Review | SellerReview) -> AdminReviewOut:
    author, subject = (
        (row.buyer_id, row.seller_id) if isinstance(row, Review) else (row.seller_id, row.buyer_id)
    )
    return AdminReviewOut(
        id=row.id,
        job_id=row.job_id,
        author_id=author,
        subject_id=subject,
        rating=row.rating,
        comment=row.comment,
        comment_hidden=row.comment_hidden,
        created_at=row.created_at,
    )


@admin_router.get("/reviews/{kind}", response_model=list[AdminReviewOut])
def admin_list_reviews(
    kind: Literal["buyer", "seller"], session: SessionDep, admin_id: AdminId
) -> list[AdminReviewOut]:
    model = Review if kind == "buyer" else SellerReview
    rows = session.scalars(select(model).order_by(model.created_at.desc())).all()
    return [_admin_review_out(r) for r in rows]


def _set_comment_hidden(
    kind: Literal["buyer", "seller"],
    review_id: UUID,
    hidden: bool,
    session: Session,
    admin_id: str,
) -> AdminReviewOut:
    model = Review if kind == "buyer" else SellerReview
    row = session.get(model, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review not found")
    if row.comment_hidden == hidden:
        raise HTTPException(status_code=409, detail="review already in that state")
    row.comment_hidden = hidden
    audit(
        session,
        admin_id,
        "hide_review" if hidden else "unhide_review",
        f"{kind}:{review_id}",
        {},
    )
    session.flush()
    return _admin_review_out(row)


@admin_router.post("/reviews/{kind}/{review_id}/hide", response_model=AdminReviewOut)
def admin_hide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, True, session, admin_id)


@admin_router.post("/reviews/{kind}/{review_id}/unhide", response_model=AdminReviewOut)
def admin_unhide_review(
    kind: Literal["buyer", "seller"], review_id: UUID, session: SessionDep, admin_id: AdminId
) -> AdminReviewOut:
    return _set_comment_hidden(kind, review_id, False, session, admin_id)


@admin_router.post("/users/{user_id}/reset_display_name", response_model=UserModerationOut)
def admin_reset_display_name(user_id: str, session: SessionDep, admin_id: AdminId) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.display_name = f"user-{user.id[:8]}"
    audit(session, admin_id, "reset_display_name", user_id, {})
    session.flush()
    return user
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: all PASS. Also run `uv run pytest tests/test_seller_reviews.py -v` — the ReviewOut/SellerReviewOut repoint must not change creation responses.

- [ ] **Step 6: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py tests/test_moderation.py
git commit -m "Add content takedown: hide and unhide review comments, admin review lists, display-name reset"
```

---

### Task 4: Reports — file, list, resolve, notify admins

**Files:**
- Modify: `src/marketplace/auth.py` (new `current_participant` dep after `current_seller` line 119)
- Modify: `src/marketplace/models.py` (report schemas)
- Modify: `src/marketplace/api.py` (reports router + admin endpoints; router registration)
- Modify: `src/marketplace/notifications.py` (renderer + RENDERERS entry)
- Test: `tests/test_moderation.py`

**Interfaces:**
- Consumes: Task 1's `Report`, `ReportTargetKind`, `ReportStatus`, `EventKind.REPORT_OPENED_ADMIN`; Task 2's `_require_active`; existing `Claims`, `Principal` (auth.py:104), `notifications.enqueue_admins`, `IntegrityError` guard pattern.
- Produces: `POST /v1/reports` → 201 `ReportOut {id, target_kind, target_id, reason, status, created_at}`; `GET /v1/reports` → reporter's own list; `GET /v1/admin/reports?status=` → `list[AdminReportOut]` (adds reporter_id, resolution_note, resolved_at); `POST /v1/admin/reports/{report_id}/resolve` body `{status: actioned|dismissed, note?}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_moderation.py` (hoist imports; `_reviewed_job` from Task 3 reused):

```python
def _report(
    client: TestClient, headers: Header, kind: str, target: str, reason: str = "abusive"
) -> "object":
    return client.post(
        "/v1/reports",
        json={"target_kind": kind, "target_id": target, "reason": reason},
        headers=headers,
    )


def test_report_eligibility_matrix(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _reviewed_job(client, basic_service, auth)  # alice <-> s1 share a job
    seller = auth("seller", "s1")
    buyer = auth("buyer", "alice")
    review_id = client.get("/v1/admin/reviews/buyer", headers=admin).json()[0]["id"]

    # counterparty user: OK
    r = _report(client, seller, "user", "alice")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "open"
    # duplicate: 409
    assert _report(client, seller, "user", "alice").status_code == 409
    # stranger: 403
    auth("buyer", "bob")
    assert _report(client, auth("seller", "s1"), "user", "bob").status_code == 403
    # self: 422
    assert _report(client, buyer, "user", "alice").status_code == 422
    # unknown user: 404
    assert _report(client, seller, "user", "ghost").status_code == 404
    # review subject reports the review: OK
    assert _report(client, seller, "review", review_id).status_code == 201
    # review author reports own review ("take my comment down"): OK
    assert _report(client, buyer, "review", review_id).status_code == 201
    # unrelated party on the review: 403
    assert _report(client, auth("buyer", "bob"), "review", review_id).status_code == 403
    # malformed review id: 404
    assert _report(client, seller, "seller_review", "not-a-uuid").status_code == 404
    # admin bearer cannot file: 403
    assert _report(client, admin, "user", "alice").status_code == 403


def test_suspended_reporter_cannot_file(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _reviewed_job(client, basic_service, auth)
    _suspend(client, admin, "s1")
    r = _report(client, auth("seller", "s1"), "user", "alice")
    assert r.status_code == 403 and r.json()["detail"] == "account suspended"


def test_report_views_and_resolve(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header, mail_outbox
) -> None:
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    report_id = _report(client, seller, "user", "alice").json()["id"]

    # admin was notified on create (RecordingEmailSender.sent is a list of
    # (to, subject, body) tuples — mail.py:33)
    from marketplace.notifications import drain_once

    drain_once(mail_outbox)
    assert any("Report filed" in sent[1] for sent in mail_outbox.sent)

    # reporter view: status only, no admin prose
    mine = client.get("/v1/reports", headers=seller).json()
    assert len(mine) == 1 and "resolution_note" not in mine[0]

    # admin filter + resolve
    assert len(client.get("/v1/admin/reports?status=open", headers=admin).json()) == 1
    assert client.get("/v1/admin/reports?status=bogus", headers=admin).status_code == 422
    r = client.post(
        f"/v1/admin/reports/{report_id}/resolve",
        json={"status": "actioned", "note": "hid the comment"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "actioned" and r.json()["resolved_at"] is not None
    # terminal: 409
    assert (
        client.post(
            f"/v1/admin/reports/{report_id}/resolve", json={"status": "dismissed"}, headers=admin
        ).status_code
        == 409
    )
    # reporter now sees the status, still not the note
    mine = client.get("/v1/reports", headers=seller).json()
    assert mine[0]["status"] == "actioned" and "resolution_note" not in mine[0]
```

NOTE: if `drain_once`'s calling convention differs from `drain_once(mail_outbox)`, mirror `tests/test_disputes.py`'s `_drain()` helper (it constructs a `RecordingEmailSender` and passes it to `drain_once`).

Plus the PG race test:

```python
@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_duplicate_report_races_to_409(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    _reviewed_job(client, basic_service, auth)
    seller = auth("seller", "s1")
    barrier = threading.Barrier(2)

    def submit(_: int) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return _report(c, seller, "user", "alice").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(submit, range(2)))
    assert codes == [201, 409], codes
    with SessionLocal() as s:
        assert len(s.scalars(select(Report)).all()) == 1
```

(hoist `threading`, `ThreadPoolExecutor`, `IS_POSTGRES`, `select`, `Report`, `from marketplace import api` to the top block.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_moderation.py -v`
Expected: new tests FAIL (no /v1/reports route). PG race test skips on SQLite.

- [ ] **Step 3: Add the participant dependency to auth.py**

After `current_seller` (line 119):

```python
def current_participant(claims: Principal) -> Claims:
    """Buyer or seller — the roles that can file reports."""
    if claims.role is UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="buyer or seller credentials required")
    return claims
```

- [ ] **Step 4: Add schemas to models.py**

After `AdminReviewOut`:

```python
class ReportRequest(BaseModel):
    target_kind: ReportTargetKind
    target_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


class ReportOut(BaseModel):
    """Reporter's view — admin prose (resolution_note) never appears here."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_kind: ReportTargetKind
    target_id: str
    reason: str
    status: ReportStatus
    created_at: datetime


class AdminReportOut(ReportOut):
    reporter_id: str
    resolution_note: str | None
    resolved_at: datetime | None


class ResolveReportRequest(BaseModel):
    status: Literal[ReportStatus.ACTIONED, ReportStatus.DISMISSED]
    note: str | None = Field(default=None, max_length=2000)
```

(models.py already imports `Literal` at line 14 — no import change needed there.)

- [ ] **Step 5: Implement the endpoints and renderer**

api.py — imports: `Claims`, `current_participant` from `.auth`; `Report` in entities block; `ReportRequest`, `ReportOut`, `AdminReportOut`, `ResolveReportRequest`, `ReportStatus`, `ReportTargetKind` in models block; `and_`, `or_` added to the `from sqlalchemy import` line (line 27).

Router, near the other router definitions (~line 300):

```python
reports_router = APIRouter(prefix="/v1", tags=["reports"])
ParticipantClaims = Annotated[Claims, Depends(current_participant)]
```

Register it with the others at api.py:1569-1573: add `app.include_router(reports_router)` after the `admin_router` line.

Endpoints (ALL reads before the `session.add` — the ordering lesson):

```python
@reports_router.post("/reports", response_model=ReportOut, status_code=201)
def file_report(body: ReportRequest, session: SessionDep, claims: ParticipantClaims) -> Report:
    reporter_id = claims.sub
    _require_active(session, reporter_id)
    if body.target_kind is ReportTargetKind.USER:
        target = session.get(User, body.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="target not found")
        if target.id == reporter_id:
            raise HTTPException(status_code=422, detail="cannot report yourself")
        shared = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                or_(
                    and_(Job.buyer_id == reporter_id, Job.seller_id == target.id),
                    and_(Job.seller_id == reporter_id, Job.buyer_id == target.id),
                )
            )
        )
        if not shared:
            raise HTTPException(status_code=403, detail="not a counterparty")
    else:
        model = Review if body.target_kind is ReportTargetKind.REVIEW else SellerReview
        try:
            review_uuid = UUID(body.target_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="target not found") from None
        row = session.get(model, review_uuid)
        if row is None:
            raise HTTPException(status_code=404, detail="target not found")
        if reporter_id not in (row.buyer_id, row.seller_id):
            raise HTTPException(status_code=403, detail="not a party to this review")

    report = Report(
        reporter_id=reporter_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        reason=body.reason,
    )
    session.add(report)
    try:
        session.flush()
    except IntegrityError:
        # Duplicate (sequential or concurrent) loses the UNIQUE race — same
        # answer either way, not a 500.
        raise HTTPException(status_code=409, detail="already reported") from None
    notifications.enqueue_admins(
        session,
        EventKind.REPORT_OPENED_ADMIN,
        {
            "report_id": str(report.id),
            "target_kind": body.target_kind,
            "target_id": body.target_id,
            "reason": body.reason,
        },
    )
    return report


@reports_router.get("/reports", response_model=list[ReportOut])
def my_reports(session: SessionDep, claims: ParticipantClaims) -> list[Report]:
    return list(
        session.scalars(
            select(Report)
            .where(Report.reporter_id == claims.sub)
            .order_by(Report.created_at.desc())
        ).all()
    )
```

Admin endpoints after `admin_reset_display_name`:

```python
@admin_router.get("/reports", response_model=list[AdminReportOut])
def admin_list_reports(
    session: SessionDep, admin_id: AdminId, status: ReportStatus | None = None
) -> list[Report]:
    q = select(Report).order_by(Report.created_at.desc())
    if status is not None:
        q = q.where(Report.status == status)
    return list(session.scalars(q).all())


@admin_router.post("/reports/{report_id}/resolve", response_model=AdminReportOut)
def resolve_report(
    report_id: UUID, body: ResolveReportRequest, session: SessionDep, admin_id: AdminId
) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    if report.status is not ReportStatus.OPEN:
        raise HTTPException(status_code=409, detail="report already resolved")
    report.status = body.status
    report.resolution_note = body.note
    report.resolved_at = _now()
    audit(
        session, admin_id, "resolve_report", str(report_id), {"status": body.status, "note": body.note or ""}
    )
    session.flush()
    return report
```

notifications.py — renderer after `_render_chargeback_closed_admin`, entry in `RENDERERS`:

```python
def _render_report_opened_admin(p: dict[str, Any]) -> tuple[str, str]:
    return (
        f"Report filed against {p['target_kind']} {p['target_id']}",
        (
            f"Reason: {p['reason']}\n"
            f"Report: {p['report_id']}\n"
            f"Review in the admin dashboard; resolving takes no automatic action."
        ),
    )
```

```python
    EventKind.REPORT_OPENED_ADMIN: _render_report_opened_admin,
```

- [ ] **Step 6: Run tests to verify they pass, then the PG-gated race on Postgres**

```bash
uv run pytest tests/test_moderation.py -v
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_moderation.py -v
```

Expected: all PASS on both (race test runs on PG only), exit 0.

- [ ] **Step 7: Lint, typecheck, full suites — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/auth.py src/marketplace/models.py src/marketplace/api.py src/marketplace/notifications.py tests/test_moderation.py
git commit -m "Add abuse reports: counterparty-only filing, admin queue and resolve, admin notification"
```

---

### Task 5: Demo act, docs, full merge gates

**Files:**
- Modify: `scripts/demo.py` (new Act 5 after act 4's dispute resolution, ~line 210)
- Modify: `ROADMAP.md`, `README.md`, `SECURITY.md`, `CLAUDE.md` (existing sections only)

**Interfaces:**
- Consumes: every endpoint from Tasks 2-4.
- Produces: demo proves the loop; docs current.

- [ ] **Step 1: Add the demo act**

After act 4's final print (~line 210), continuing the numbered steps (act 4 ends at step 14):

```python
    # --- Act 5: moderation (report -> takedown -> suspension -> reinstate) ---
    print("15. Carol reports Alice's review; the admin queue lights up")
    review_id = c.get("/v1/admin/reviews/buyer", headers=admin).json()[0]["id"]
    report = c.post(
        "/v1/reports",
        json={"target_kind": "review", "target_id": review_id, "reason": "abusive language"},
        headers=carol,
    ).json()
    print(f"   report status = {report['status']}")

    print("16. Admin hides the comment (rating and aggregates stay)")
    hidden = c.post(f"/v1/admin/reviews/buyer/{review_id}/hide", headers=admin).json()
    print(f"   comment_hidden = {hidden['comment_hidden']}")

    print("17. Admin suspends Alice — acquisition blocked, reads still fine")
    c.post(f"/v1/admin/users/{alice_id}/suspend", json={"reason": "abuse"}, headers=admin)
    blocked = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice)
    print(f"   new quote -> {blocked.status_code} {blocked.json()['detail']}")
    assert blocked.status_code == 403

    print("18. Reinstate + resolve the report (no automatic actions either way)")
    c.post(f"/v1/admin/users/{alice_id}/reinstate", headers=admin)
    resolved = c.post(
        f"/v1/admin/reports/{report['id']}/resolve",
        json={"status": "actioned", "note": "comment hidden"},
        headers=admin,
    ).json()
    print(f"   report -> {resolved['status']}")
    assert resolved["status"] == "actioned"
```

NOTE: act 4 opens a dispute on job 1 — the review from step 7 exists, so the admin list has at least one buyer review; index [0] is newest-first. Verify `alice_id`/`carol` variable names near line 80 and reuse them. If step numbers shifted, continue from the actual last number.

- [ ] **Step 2: Run the demo**

Run: `uv run python scripts/demo.py`
Expected: exit 0, steps 15-18 print.

- [ ] **Step 3: Update docs (existing sections only, current formatting)**

- `ROADMAP.md`: fold moderation into Done (style-match the disputes/seller-reviews entries: suspension verb-gating table's essence in two lines, takedown hide-not-delete, counterparty reports, no auto-actions); T&S remaining shrinks to "notification preferences"; keep "abuse signals/limits" listed as deferred with one line saying why (fork-specific heuristics).
- `SECURITY.md`: bullets for the moderation asymmetries — reporter never sees resolution_note; suspension surfaces to the suspended user only as the 403 detail; hidden comments stay admin-visible; admins cannot be suspended.
- `README.md`: add the new endpoints to the existing endpoint lists (buyer: GET /v1/reports + POST /v1/reports shared with seller — place per the file's existing structure; admin: users suspend/reinstate/reset_display_name, reviews list/hide/unhide, reports list/resolve).
- `CLAUDE.md`: update only if it enumerates endpoints/tables/migrations (it does mention migration count — make it 7).

- [ ] **Step 4: Full gates — bare exit codes, both backends, fresh-volume migrations**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
uv run python scripts/demo.py
docker compose down -v db && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
```

Expected: every command exits 0; 7 migrations from scratch; no new SQLite skips beyond the PG-gated ones.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py ROADMAP.md README.md SECURITY.md CLAUDE.md
git commit -m "Document moderation: demo act 5, roadmap, security notes"
```

---

## Self-review notes (already applied)

- Spec coverage: schema/migration → Task 1; suspension incl. matching anti-join → Task 2; takedown incl. the new admin review lists + invariant repoint → Task 3; reports incl. participant dep, eligibility matrix, notification → Task 4; demo/docs/gates → Task 5. Non-goals untouched (no auto-actions anywhere; resolve_report only writes report fields).
- The ordering lesson is a Global Constraint and file_report's reads all precede `session.add`.
- Type consistency: `_require_active(session, user_id)` used identically in Tasks 2 and 4; `UserModerationOut` (with display_name) shared by suspend/reinstate/reset; `AdminReviewOut` construction helper reused by list/hide/unhide.
