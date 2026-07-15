# Notification Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-kind notification mutes with a money-only must-send floor, enforced at enqueue time.

**Architecture:** Sparse `notification_mutes` table (row = muted). Two constants in notifications.py (`MUST_SEND`, `KIND_ROLES`) drive enforcement in the existing `enqueue`/`enqueue_admins` chokepoints and the role-scoped GET/PUT `/v1/notification-preferences` endpoints. PUT is replace-set, serialized per user by a `FOR UPDATE` lock on the caller's User row (naive delete-then-insert unions concurrent sets under READ COMMITTED). Migration #8.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, pytest. `uv run` everything.

**Spec:** `docs/superpowers/specs/2026-07-14-notification-preferences-design.md` — read it first.

## Global Constraints

- Python via `uv` only. Gate on BARE exit codes — never pipe a checker (`pytest | tail` masks the exit).
- Tests default to SQLite; Postgres: `docker compose up -d db` then `DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest` (`sg docker -c "..."` if docker denied).
- Pyright strict must end 0 errors; no print() in src/ (demo prints by design).
- PostToolUse formatter strips not-yet-used imports — add import + first use in one edit.
- Ordering lesson: reads before `session.add`; the guarded flush is the only statement that can hit a UNIQUE violation.
- MUST_SEND is exactly: `REFUND_ISSUED_BUYER`, `DISPUTE_RESOLVED_BUYER`, `DISPUTE_RESOLVED_SELLER`, `PAYOUT_FAILED_ADMIN`. These four kinds never consult the mute table — a smuggled DB mute row for them is ignored.
- Preferences endpoints are NOT suspension-gated (preference ≠ acquisition verb) and serve all three roles via `Principal`.
- Commit style: concise, imperative; end body with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No backticks in double-quoted -m strings.

---

### Task 1: NotificationMute entity, MUST_SEND/KIND_ROLES constants, migration #8

**Files:**
- Modify: `src/marketplace/entities.py` (new entity after `Notification` ~line 420)
- Modify: `src/marketplace/notifications.py` (constants after the imports/logger, before `enqueue`)
- Create: `migrations/versions/<autogen>_notification_prefs.py`
- Test: `tests/test_notification_prefs.py` (new)

**Interfaces:**
- Consumes: `_enum`, `_TS`, `_now`, `UniqueConstraint`, `EventKind`, `UserRole` — all existing.
- Produces: entity `NotificationMute(user_id: str indexed, kind: EventKind, UNIQUE(user_id, kind))`; `notifications.MUST_SEND: frozenset[EventKind]` (the four money kinds); `notifications.KIND_ROLES: dict[EventKind, UserRole]` (all 14 kinds → recipient role). Tasks 2-3 import all three.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notification_prefs.py`:

```python
"""Notification preferences: per-kind mutes, money-only must-send floor.
Spec: 2026-07-14-notification-preferences-design.md."""

from marketplace.models import EventKind, UserRole


def test_mutes_table_registered() -> None:
    from marketplace.entities import Base

    assert "notification_mutes" in Base.metadata.tables


def test_kind_roles_covers_every_kind() -> None:
    """A future kind added without a role mapping must fail fast (same
    pattern as the every-kind-has-a-renderer invariant)."""
    from marketplace.notifications import KIND_ROLES

    assert set(KIND_ROLES) == set(EventKind)
    assert set(KIND_ROLES.values()) <= {UserRole.BUYER, UserRole.SELLER, UserRole.ADMIN}


def test_must_send_is_the_money_floor() -> None:
    from marketplace.notifications import KIND_ROLES, MUST_SEND

    assert MUST_SEND == {
        EventKind.REFUND_ISSUED_BUYER,
        EventKind.DISPUTE_RESOLVED_BUYER,
        EventKind.DISPUTE_RESOLVED_SELLER,
        EventKind.PAYOUT_FAILED_ADMIN,
    }
    assert MUST_SEND <= set(KIND_ROLES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notification_prefs.py -v`
Expected: FAIL — no table, no `KIND_ROLES`, no `MUST_SEND`.

- [ ] **Step 3: Add the entity**

`src/marketplace/entities.py`, after `Notification`:

```python
class NotificationMute(Base):
    """Sparse per-user opt-out: a row means muted, absence means subscribed.
    Money kinds (MUST_SEND in notifications.py) never consult this table."""

    __tablename__ = "notification_mutes"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[EventKind] = mapped_column(_enum(EventKind))
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
```

- [ ] **Step 4: Add the constants**

`src/marketplace/notifications.py`, module level, after the logger line and before `enqueue` (extend the existing `from .models import` block with `UserRole` if it is not already imported — it is used by `enqueue_admins`, check first):

```python
# The floor: mail that records money movement can never be muted.
MUST_SEND: frozenset[EventKind] = frozenset(
    {
        EventKind.REFUND_ISSUED_BUYER,
        EventKind.DISPUTE_RESOLVED_BUYER,
        EventKind.DISPUTE_RESOLVED_SELLER,
        EventKind.PAYOUT_FAILED_ADMIN,
    }
)

# Recipient role per kind. Explicit beats deriving from name suffixes; the
# coverage test fails fast when a kind is added without a mapping.
KIND_ROLES: dict[EventKind, UserRole] = {
    EventKind.OFFER_RECEIVED: UserRole.SELLER,
    EventKind.JOB_ACCEPTED_BUYER: UserRole.BUYER,
    EventKind.JOB_COMPLETED_BUYER: UserRole.BUYER,
    EventKind.JOB_EXPIRED_BUYER: UserRole.BUYER,
    EventKind.JOB_CANCELLED_SELLER: UserRole.SELLER,
    EventKind.REFUND_ISSUED_BUYER: UserRole.BUYER,
    EventKind.PAYOUT_FAILED_ADMIN: UserRole.ADMIN,
    EventKind.DISPUTE_OPENED_SELLER: UserRole.SELLER,
    EventKind.DISPUTE_OPENED_ADMIN: UserRole.ADMIN,
    EventKind.DISPUTE_RESOLVED_BUYER: UserRole.BUYER,
    EventKind.DISPUTE_RESOLVED_SELLER: UserRole.SELLER,
    EventKind.CHARGEBACK_OPENED_ADMIN: UserRole.ADMIN,
    EventKind.CHARGEBACK_CLOSED_ADMIN: UserRole.ADMIN,
    EventKind.REPORT_OPENED_ADMIN: UserRole.ADMIN,
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notification_prefs.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Generate migration #8**

Postgres up first (`docker compose up -d db`), then:

```bash
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic revision --autogenerate -m "notification prefs"
```

The new table has no NOT NULL additions to existing tables — no hand-added server defaults needed this time. Verify the autogenerated file creates `notification_mutes` with the UNIQUE constraint and the `ix_notification_mutes_user_id` index, nothing else.

- [ ] **Step 7: Round-trip the migration (gate is 8)**

```bash
docker compose down -v db && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic downgrade -1
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic history
```

Expected: all exit 0; history shows 8 revisions.

- [ ] **Step 8: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0.

- [ ] **Step 9: Commit**

```bash
git add src/marketplace/entities.py src/marketplace/notifications.py migrations/versions tests/test_notification_prefs.py
git commit -m "Add notification mute schema and kind constants (migration #8)"
```

---

### Task 2: Enqueue-time enforcement

**Files:**
- Modify: `src/marketplace/notifications.py` (`enqueue` line ~36, `enqueue_admins` line ~46)
- Test: `tests/test_notification_prefs.py`

**Interfaces:**
- Consumes: Task 1's `NotificationMute`, `MUST_SEND`.
- Produces: `enqueue` skips (debug log) when `kind not in MUST_SEND` and a mute row exists; `enqueue_admins` filters muted admins with ONE query. Task 3's PUT tests rely on this behavior end-to-end.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notification_prefs.py` (white-box: drive `enqueue` directly with a session; the `auth` fixture materializes users):

```python
from sqlalchemy import select

from marketplace.db import SessionLocal
from marketplace.entities import Notification, NotificationMute
from marketplace.notifications import enqueue, enqueue_admins
from tests.conftest import AuthFactory, Header


def _mute(user_id: str, kind: EventKind) -> None:
    with SessionLocal() as s:
        s.add(NotificationMute(user_id=user_id, kind=kind))
        s.commit()


def _outbox_kinds(user_id: str) -> list[str]:
    with SessionLocal() as s:
        rows = s.scalars(select(Notification).where(Notification.user_id == user_id)).all()
        return [str(r.kind) for r in rows]


def test_enqueue_skips_muted_kind(auth: AuthFactory) -> None:
    auth("seller", "s1")
    _mute("s1", EventKind.OFFER_RECEIVED)
    with SessionLocal() as s:
        enqueue(s, EventKind.OFFER_RECEIVED, "s1", {"job_id": "j"})
        s.commit()
    assert _outbox_kinds("s1") == []


def test_enqueue_ignores_smuggled_mute_on_money_kind(auth: AuthFactory) -> None:
    """The floor is server-side: even a directly-inserted mute row for a
    money kind must not suppress the mail."""
    auth("buyer", "alice")
    _mute("alice", EventKind.REFUND_ISSUED_BUYER)
    with SessionLocal() as s:
        enqueue(s, EventKind.REFUND_ISSUED_BUYER, "alice", {"job_id": "j", "buyer_price": "1.00"})
        s.commit()
    assert _outbox_kinds("alice") == ["refund_issued_buyer"]


def test_enqueue_admins_filters_only_muted_admin(auth: AuthFactory) -> None:
    auth("admin", "adm1")
    auth("admin", "adm2")
    _mute("adm1", EventKind.REPORT_OPENED_ADMIN)
    with SessionLocal() as s:
        enqueue_admins(
            s,
            EventKind.REPORT_OPENED_ADMIN,
            {
                "report_id": "r",
                "target_kind": "user",
                "target_id": "x",
                "reason": "spam",
                "reporter_id": "y",
            },
        )
        s.commit()
    assert _outbox_kinds("adm1") == []
    assert _outbox_kinds("adm2") == ["report_opened_admin"]
```

NOTE: the seeded lifespan admin does not exist in tests (client fixture skips lifespan), but other tests may create admin users; these white-box tests call `enqueue_admins` directly in their own session so only `adm1`/`adm2` (plus the `admin` fixture's user if the fixture is pulled in — it is NOT pulled in here, `auth` only) exist. If a stray admin from test pollution appears, the `clean_tables` autouse fixture guarantees isolation between tests — no action needed, just don't add the `admin` fixture to these tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notification_prefs.py -v`
Expected: the three new tests FAIL (mute rows don't suppress anything yet).

- [ ] **Step 3: Implement the filters**

`enqueue` — after the missing-user skip (line ~42), before `session.add`:

```python
    if kind not in MUST_SEND and (
        session.scalar(
            select(NotificationMute.id).where(
                NotificationMute.user_id == user.id, NotificationMute.kind == kind
            )
        )
        is not None
    ):
        logger.debug("notification %s muted by user %s", kind, user_id)
        return
```

`enqueue_admins` — replace the loop body:

```python
    admins = session.scalars(select(User).where(User.role == UserRole.ADMIN)).all()
    if not admins:
        logger.warning("notification %s skipped: no admin accounts", kind)
        return
    muted: set[str] = set()
    if kind not in MUST_SEND:
        muted = set(
            session.scalars(
                select(NotificationMute.user_id).where(
                    NotificationMute.kind == kind,
                    NotificationMute.user_id.in_([a.id for a in admins]),
                )
            ).all()
        )
    for admin in admins:
        if admin.id in muted:
            logger.debug("notification %s muted by admin %s", kind, admin.id)
            continue
        session.add(Notification(user_id=admin.id, email=admin.email, kind=kind, payload=payload))
```

Extend notifications.py's `from .entities import` block with `NotificationMute`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_notification_prefs.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, typecheck, full SQLite suite — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Expected: all exit 0 (no existing notification test regresses — no mutes exist unless a test creates them).

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/notifications.py tests/test_notification_prefs.py
git commit -m "Enforce notification mutes at enqueue time, money kinds exempt"
```

---

### Task 3: GET/PUT /v1/notification-preferences

**Files:**
- Modify: `src/marketplace/models.py` (two schemas after `AdminReportOut`)
- Modify: `src/marketplace/api.py` (new `prefs_router` near the other router definitions ~line 320; registration in the include_router block ~line 1830)
- Test: `tests/test_notification_prefs.py`

**Interfaces:**
- Consumes: Task 1's constants + entity; Task 2's enforcement (for end-to-end assertions); `Principal` from auth.py:104 (add to api.py's `from .auth import` block); `Claims`; existing `SessionDep`, `IntegrityError`, `delete`.
- Produces: `GET /v1/notification-preferences` → `list[NotificationPreferenceOut {kind, muted, locked}]` (caller's role only, KIND_ROLES declaration order); `PUT /v1/notification-preferences` body `{"muted": [...]}` replace-set → same shape; 422 must-send/off-role; last-writer-wins under concurrency.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notification_prefs.py` (hoist all imports to the top block; `threading`/`ThreadPoolExecutor`/`IS_POSTGRES`/`api`/`TestClient`/`pytest` as needed; reuse `onboard_and_avail`, `new_job` from `tests.test_payments`):

```python
def test_get_defaults_and_role_scoping(client: TestClient, auth: AuthFactory, admin: Header) -> None:
    buyer_rows = client.get("/v1/notification-preferences", headers=auth("buyer", "alice")).json()
    assert {r["kind"] for r in buyer_rows} == {
        "job_accepted_buyer",
        "job_completed_buyer",
        "job_expired_buyer",
        "refund_issued_buyer",
        "dispute_resolved_buyer",
    }
    assert all(r["muted"] is False for r in buyer_rows)
    locked = {r["kind"] for r in buyer_rows if r["locked"]}
    assert locked == {"refund_issued_buyer", "dispute_resolved_buyer"}

    seller_rows = client.get("/v1/notification-preferences", headers=auth("seller", "s1")).json()
    assert len(seller_rows) == 4
    admin_rows = client.get("/v1/notification-preferences", headers=admin).json()
    assert len(admin_rows) == 5


def test_put_replace_set_and_end_to_end_mute(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    seller = auth("seller", "s1")

    r = client.put("/v1/notification-preferences", json={"muted": ["offer_received"]}, headers=seller)
    assert r.status_code == 200
    assert {x["kind"]: x["muted"] for x in r.json()}["offer_received"] is True

    new_job(client, auth, basic_service, "alice")  # matches s1, offer created
    assert _outbox_kinds("s1") == []  # nudge suppressed
    # in-app unaffected: the offer row exists
    assert len(client.get("/v1/seller/offers", headers=seller).json()) == 1

    # replace-set: PUT a different set unmutes offer_received
    r = client.put(
        "/v1/notification-preferences", json={"muted": ["job_cancelled_seller"]}, headers=seller
    )
    assert r.status_code == 200
    new_job(client, auth, basic_service, "alice")
    assert "offer_received" in _outbox_kinds("s1")


def test_put_rejects_must_send_off_role_and_unknown(
    client: TestClient, auth: AuthFactory
) -> None:
    buyer = auth("buyer", "alice")
    for bad in (["refund_issued_buyer"], ["offer_received"], ["not_a_kind"]):
        r = client.put("/v1/notification-preferences", json={"muted": bad}, headers=buyer)
        assert r.status_code == 422, bad
    # and the failed PUTs changed nothing
    rows = client.get("/v1/notification-preferences", headers=buyer).json()
    assert all(x["muted"] is False for x in rows)


def test_put_is_idempotent_and_tolerates_duplicates(client: TestClient, auth: AuthFactory) -> None:
    buyer = auth("buyer", "alice")
    body = {"muted": ["job_accepted_buyer", "job_accepted_buyer"]}
    assert client.put("/v1/notification-preferences", json=body, headers=buyer).status_code == 200
    assert client.put("/v1/notification-preferences", json=body, headers=buyer).status_code == 200
    with SessionLocal() as s:
        rows = s.scalars(select(NotificationMute).where(NotificationMute.user_id == "alice")).all()
        assert len(rows) == 1


@pytest.mark.skipif(not IS_POSTGRES, reason="true-parallel writes are only real on Postgres")
def test_concurrent_puts_last_writer_wins(client: TestClient, auth: AuthFactory) -> None:
    """Naive delete-then-insert unions concurrent sets; the FOR UPDATE user
    lock serializes them so the result is exactly one caller's set."""
    buyer = auth("buyer", "alice")
    set_a = ["job_accepted_buyer"]
    set_b = ["job_completed_buyer", "job_expired_buyer"]
    barrier = threading.Barrier(2)

    def put(muted: list[str]) -> int:
        c = TestClient(api.app)
        barrier.wait()
        return c.put("/v1/notification-preferences", json={"muted": muted}, headers=buyer).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(put, [set_a, set_b]))
    assert all(code in (200, 409) for code in codes), codes
    with SessionLocal() as s:
        kinds = sorted(
            str(m.kind) for m in s.scalars(
                select(NotificationMute).where(NotificationMute.user_id == "alice")
            ).all()
        )
    assert kinds in (sorted(set_a), sorted(set_b)), kinds
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notification_prefs.py -v`
Expected: new tests FAIL (routes don't exist); PG test skips on SQLite.

- [ ] **Step 3: Add the schemas**

`src/marketplace/models.py`, after `AdminReportOut`:

```python
class NotificationPreferenceOut(BaseModel):
    kind: EventKind
    muted: bool
    locked: bool  # must-send: always sent, cannot be muted


class NotificationPreferencesUpdate(BaseModel):
    muted: list[EventKind]
```

- [ ] **Step 4: Implement the endpoints**

`src/marketplace/api.py` — add `Principal` to the `from .auth import` block; `NotificationMute` to the entities block; `NotificationPreferenceOut`, `NotificationPreferencesUpdate` to the models block. Near the router definitions (~line 320):

```python
prefs_router = APIRouter(prefix="/v1", tags=["preferences"])


def _pref_rows(session: Session, claims: Claims) -> list[NotificationPreferenceOut]:
    kinds = [k for k, role in notifications.KIND_ROLES.items() if role is claims.role]
    muted = set(
        session.scalars(
            select(NotificationMute.kind).where(NotificationMute.user_id == claims.sub)
        ).all()
    )
    return [
        NotificationPreferenceOut(
            kind=k,
            muted=k in muted and k not in notifications.MUST_SEND,
            locked=k in notifications.MUST_SEND,
        )
        for k in kinds
    ]


@prefs_router.get("/notification-preferences", response_model=list[NotificationPreferenceOut])
def get_notification_preferences(
    session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    return _pref_rows(session, claims)


@prefs_router.put("/notification-preferences", response_model=list[NotificationPreferenceOut])
def put_notification_preferences(
    body: NotificationPreferencesUpdate, session: SessionDep, claims: Principal
) -> list[NotificationPreferenceOut]:
    wanted = set(body.muted)
    for kind in wanted:
        if kind in notifications.MUST_SEND:
            raise HTTPException(status_code=422, detail=f"{kind} cannot be muted")
        if notifications.KIND_ROLES[kind] is not claims.role:
            raise HTTPException(status_code=422, detail=f"{kind} is not a {claims.role} kind")
    # Serialize concurrent PUTs for the same user: naive delete-then-insert
    # under READ COMMITTED unions both sets (each DELETE misses the other's
    # uncommitted rows). The row lock makes replace-set last-writer-wins.
    session.get(User, claims.sub, with_for_update=True)
    session.execute(delete(NotificationMute).where(NotificationMute.user_id == claims.sub))
    for kind in sorted(wanted):
        session.add(NotificationMute(user_id=claims.sub, kind=kind))
    try:
        session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="preferences changed concurrently, retry"
        ) from None
    return _pref_rows(session, claims)
```

Register in the include_router block: `app.include_router(prefs_router)` after `reports_router`.

Notes for the implementer:
- `notifications` is already imported as a module in api.py (`from . import notifications` or equivalent — check the import line; constants are reached as `notifications.MUST_SEND`).
- Unknown kind strings never reach the handler — pydantic's `EventKind` validation 422s them (the test's `"not_a_kind"` case).
- The `session.get(..., with_for_update=True)` here is a pure mutex — no mid-request commit precedes it, so the stale-attribute relock trap (see progress.md LESSON) does not apply; do not add populate_existing.

- [ ] **Step 5: Run tests to verify they pass, then the PG race on Postgres**

```bash
uv run pytest tests/test_notification_prefs.py -v
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest tests/test_notification_prefs.py -v
```

Expected: all PASS on both; race test runs on PG.

- [ ] **Step 6: Lint, typecheck, full suites — bare exit codes**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/models.py src/marketplace/api.py tests/test_notification_prefs.py
git commit -m "Add notification preference endpoints: role-scoped GET, race-safe replace-set PUT"
```

---

### Task 4: Demo act 6, docs, full merge gates

**Files:**
- Modify: `scripts/demo.py` (after act 5's last step — currently step 18 at ~line 244; verify the actual last number and continue)
- Modify: `ROADMAP.md`, `README.md`, `SECURITY.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: demo proves mute → silent match → in-app offer intact → unmute → mail resumes; docs record T&S COMPLETE.

- [ ] **Step 1: Add the demo act**

After act 5's final block:

```python
    # --- Act 6: notification preferences (mute the nudge, money mail stays) ---
    print("19. Carol mutes offer_received — a new job matches silently")
    c.put("/v1/notification-preferences", json={"muted": ["offer_received"]}, headers=carol)

    def offer_mail_count() -> int:
        rows = c.get("/v1/admin/notifications", headers=admin).json()
        return len([n for n in rows if n["kind"] == "offer_received"])

    before = offer_mail_count()
    q = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    c.post("/v1/jobs", json={"quote_id": q["id"]}, headers=alice)
    assert offer_mail_count() == before, "muted offer still mailed"
    offers = c.get("/v1/seller/offers", headers=carol).json()
    print(f"   offer mails unchanged ({before}); in-app offers visible: {len(offers)}")
    assert offers, "offer should still exist in-app"

    print("20. Carol unmutes — the next offer mails again")
    c.put("/v1/notification-preferences", json={"muted": []}, headers=carol)
    q = c.post("/v1/quotes", json={"service_type_id": sid}, headers=alice).json()
    c.post("/v1/jobs", json={"quote_id": q["id"]}, headers=alice)
    assert offer_mail_count() == before + 1
    print("   offer mail queued after unmute")
```

NOTE: verify carol has spare capacity at this point in the demo (capacity 2; count her non-completed accepted jobs in acts 1-5 — completed jobs free the slot). If both new jobs can't get offers because an earlier pending offer holds... offers don't consume capacity, accepted jobs do; act 3's job was left with a pending offer to carol, which may have EXPIRED by now (2-minute clock, demo runs in seconds — it has not expired; carol may have 2 offers outstanding, which is fine). If the second job in this act gets no offer because the first act-6 job's offer is outstanding and capacity math blocks it, accept/complete the first act-6 offer before step 20 — adapt with asserts, keep the two mute/unmute assertions intact.

- [ ] **Step 2: Run the demo**

Run: `uv run python scripts/demo.py`
Expected: exit 0, steps 19-20 print with both assertions passing.

- [ ] **Step 3: Update the docs**

- `ROADMAP.md`: T&S item → COMPLETE (all four sub-phases done; list them one line each in the Done style); abuse signals/limits stays as its own deferred line (fork-specific heuristics).
- `SECURITY.md`: bullet in (or adjacent to) the moderation update section: notification mutes are per-kind with a server-side money floor — `refund_issued_buyer`, `dispute_resolved_buyer`, `dispute_resolved_seller`, `payout_failed_admin` cannot be muted even by direct DB rows; enforcement is at enqueue, so the outbox is what will actually send.
- `README.md`: add `GET/PUT /v1/notification-preferences` to the endpoint lists (it serves all roles — place it per the file's structure, e.g. a shared/account section or noted under each).
- `CLAUDE.md`: migration count 7 → 8 in the moderation summary sentence added last branch.

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

Expected: every command exits 0; 8 migrations from scratch.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py ROADMAP.md README.md SECURITY.md CLAUDE.md
git commit -m "Document notification preferences: demo act 6, T&S bucket complete"
```

---

## Self-review notes (already applied)

- Spec coverage: entity/constants/migration → Task 1; enqueue enforcement (incl. smuggled-row floor + admin filter) → Task 2; GET/PUT with role scoping, replace-set, 422s, race → Task 3; demo/docs/gates → Task 4. Non-goals untouched.
- The union-under-concurrency trap is called out where it bites (Task 3 Step 4) with the FOR UPDATE mutex and an explicit note that the stale-relock lesson does NOT apply (no mid-request commit).
- Type consistency: `MUST_SEND`/`KIND_ROLES` referenced as `notifications.` attributes in api.py, direct imports in tests; `NotificationPreferenceOut {kind, muted, locked}` identical in Tasks 3 code and tests.
