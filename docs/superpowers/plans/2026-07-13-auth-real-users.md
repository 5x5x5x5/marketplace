# Auth: Real Users Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pilot HMAC tokens with a real user store — signup, login, DB-backed revocable sessions, email verification, and password reset — so a fork can pilot with self-service users.

**Architecture:** `users` / `auth_sessions` / `email_tokens` tables (Alembic migration #3). The principal seam (`current_buyer`/`current_seller`/`require_admin`) keeps its exact signatures but resolves bearer tokens via one indexed session lookup instead of HMAC verification. An `EmailSender` port with a console dev adapter carries verification/reset mail. The entire HMAC path (`mint_token`, `_verify`, `MARKETPLACE_SECRET`) is deleted — one trust mechanism remains.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + Alembic, `pwdlib[argon2]` (new dep), `pydantic[email]` (new dep for EmailStr), pytest on SQLite (Postgres via `DATABASE_URL`).

**Spec:** `docs/superpowers/specs/2026-07-13-auth-real-users-design.md` (approved). Branch: `auth-real-users`.

## Global Constraints

- Package manager is `uv` — `uv run pytest`, `uv add`, never pip/venv.
- Gate after every task: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` — all green, pyright **strict**.
- **Never gate a commit/merge on a piped test command** (`pytest | tail` returns tail's exit code). Run the gate bare, or check `$?` explicitly.
- The PostToolUse formatter deletes not-yet-used imports. Add an import in the same edit as the code that uses it; if an import vanishes, re-add it after the usage lands.
- No backticks in double-quoted `git commit -m`. Stage exact paths, never `git add -A`. End commit messages with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Identity comes from the authenticated principal, never a request body.
- ORM entities never leave the API layer (Pydantic views only).
- The pricing/matching core (`pricing.py`, `matching.py`, `config.py`) and all payments code paths stay untouched except where this plan names them.
- **Zero churn in existing test files**, with exactly one named exception: `tests/test_auth_and_hardening.py::test_expired_token_rejected` tests the dying HMAC mechanism and is replaced by a session-expiry equivalent (Task 3).
- Tests run on SQLite by default (`tests/conftest.py` pins env before app import — the Stripe pins added there must survive); `with_for_update` is a no-op on SQLite and real on Postgres.
- `User.id` is a **String(128)** primary key (uuid4 hex for real signups) — deliberately, so the test fixture can use the test's plain sub string ("alice") as the id and all existing identity-based assertions keep working.

---

### Task 1: Mail port + auth settings + new dependencies

**Files:**
- Create: `src/marketplace/mail.py`
- Modify: `src/marketplace/settings.py`
- Modify: `pyproject.toml` (via `uv add`)
- Test: `tests/test_auth.py` (new file, first tests)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (later tasks import these exactly):
  - `marketplace.mail`: protocol `EmailSender` (method `send(self, to: str, subject: str, body: str) -> None`), `ConsoleEmailSender`, `RecordingEmailSender` (attr `sent: list[tuple[str, str, str]]`), `get_mail_sender() -> EmailSender`, `use_sender(sender: EmailSender) -> EmailSender` (returns the previous sender).
  - `settings`: `session_ttl_hours: int = 72`, `admin_email: str = ""`, `admin_password: str = ""`, `base_url: str = "http://localhost:8000"`.
  - Installed deps: `pwdlib[argon2]`, `email-validator` (via `pydantic[email]`).

- [ ] **Step 1: Add the dependencies**

Run: `uv add "pwdlib[argon2]" "pydantic[email]"`
Expected: both added to `[project.dependencies]`, lockfile updated.

- [ ] **Step 2: Write the failing test**

Create `tests/test_auth.py`:

```python
"""Real-user auth: mail port, signup/login/sessions, verify + reset flows."""

from marketplace.mail import ConsoleEmailSender, RecordingEmailSender, get_mail_sender, use_sender


def test_mail_sender_swap_roundtrip() -> None:
    recorder = RecordingEmailSender()
    previous = use_sender(recorder)
    try:
        assert get_mail_sender() is recorder
        get_mail_sender().send("a@b.test", "hi", "body")
        assert recorder.sent == [("a@b.test", "hi", "body")]
    finally:
        use_sender(previous)
    assert isinstance(get_mail_sender(), ConsoleEmailSender)


def test_console_sender_logs_instead_of_sending() -> None:
    # The dev adapter must never raise — it only logs.
    ConsoleEmailSender().send("a@b.test", "subject", "body")
```

Run: `uv run pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marketplace.mail'`.

- [ ] **Step 3: Create `src/marketplace/mail.py`**

```python
"""Outbound-email port — the seam between the app and mail delivery.

The console adapter logs instead of sending, keeping dev/tests turnkey; forks
plug in SES/Resend/Postmark behind the same protocol. The notifications phase
builds on this port.
"""

import logging
from typing import Protocol

logger = logging.getLogger("marketplace.mail")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Dev adapter: the 'sent' mail lands in the log."""

    def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email to=%s subject=%r body=%r", to, subject, body)


class RecordingEmailSender:
    """Test double: captures sends so tests read tokens from the port, not logs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


_active: EmailSender = ConsoleEmailSender()


def get_mail_sender() -> EmailSender:
    return _active


def use_sender(sender: EmailSender) -> EmailSender:
    """Swap the active sender (tests, forks); returns the previous one."""
    global _active
    previous = _active
    _active = sender
    return previous
```

- [ ] **Step 4: Extend `src/marketplace/settings.py`**

After the payments block inside `Settings`:

```python
    # Auth. Sessions are DB-backed and revocable; the admin account is seeded
    # from these two settings at startup (empty -> no admin, logged).
    session_ttl_hours: int = 72
    admin_email: str = ""
    admin_password: str = ""
    base_url: str = "http://localhost:8000"  # used in verification/reset links
```

(Do NOT remove `marketplace_secret`/`token_ttl_hours` yet — the HMAC path still uses them until Task 3.)

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/test_auth.py -q` → 2 passed.
Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → all green (existing 101 collected: 100 passed + 1 skip).

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/mail.py src/marketplace/settings.py tests/test_auth.py pyproject.toml uv.lock
git commit -m "Add outbound-email port and auth settings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Identity entities + Alembic migration #3

**Files:**
- Modify: `src/marketplace/models.py` (two enums)
- Modify: `src/marketplace/entities.py`
- Create: `migrations/versions/<autogen>_auth.py` (via alembic autogenerate)
- Test: `tests/test_auth.py` (append one schema test)

**Interfaces:**
- Consumes: `_enum(type[StrEnum])`, `_TS`, `Base`, `_now` from entities (all exist).
- Produces: `models.UserRole` (`BUYER/SELLER/ADMIN` = "buyer"/"seller"/"admin"), `models.EmailTokenPurpose` (`VERIFY/RESET` = "verify"/"reset"); entities `User` (String(128) pk, unique (email, role)), `AuthSession` (token_hash String(64) unique), `EmailToken` (token_hash unique, used_at nullable).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auth.py`:

```python
def test_auth_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"users", "auth_sessions", "email_tokens"} <= set(Base.metadata.tables)
```

Run: `uv run pytest tests/test_auth.py::test_auth_tables_registered -q`
Expected: FAIL — assertion error.

- [ ] **Step 2: Add enums to `src/marketplace/models.py`**

Below `OfferStatus`:

```python
class UserRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    ADMIN = "admin"  # seeded from settings, never self-signup


class EmailTokenPurpose(StrEnum):
    VERIFY = "verify"
    RESET = "reset"
```

- [ ] **Step 3: Add entities to `src/marketplace/entities.py`**

Extend the `.models` import: `from .models import JobStatus, OfferStatus, PaymentStatus, PayoutStatus, UserRole, EmailTokenPurpose` (match existing import style; the formatter will sort).

After `IdempotencyRecord`:

```python
class User(Base):
    """Identity + credential. One account carries exactly ONE role; the same
    email may register once per role. Domain records (Buyer/SellerProfile)
    stay separate and are keyed by this id.

    String pk (uuid4 hex for real signups) so the test fixture can use the
    test's plain sub string as the id — existing identity assertions hold."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", "role"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=lambda: uuid.uuid4().hex)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole))
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(128))
    email_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class AuthSession(Base):
    """A revocable login. Stores only the sha256 of the opaque bearer —
    a DB leak never yields usable tokens. Logout/ban/reset delete rows."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    expires_at: Mapped[datetime] = mapped_column(_TS)


class EmailToken(Base):
    """Single-use verification/reset token (sha256-stored, like sessions)."""

    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[EmailTokenPurpose] = mapped_column(_enum(EmailTokenPurpose))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(_TS)
    used_at: Mapped[datetime | None] = mapped_column(_TS, default=None)
```

- [ ] **Step 4: Run the schema test**

Run: `uv run pytest tests/test_auth.py -q` → all pass.

- [ ] **Step 5: Generate + verify the migration**

```bash
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic revision --autogenerate -m "auth"
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
```

Verify in the generated file: three new tables; `UTCDateTime` columns render as `sa.DateTime(timezone=True)`; no `marketplace.` import; unique constraints on `(email, role)`, `auth_sessions.token_hash`, `email_tokens.token_hash`. All columns are on NEW tables, so no server-default hand-fixes are needed this time. Final `upgrade head` from scratch exits 0 with all three migrations.

- [ ] **Step 6: Full gate + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → green.

```bash
git add src/marketplace/models.py src/marketplace/entities.py migrations/versions/ tests/test_auth.py
git commit -m "Add User, AuthSession, EmailToken entities and migration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: The trust-path swap (HMAC dies; suite stays green)

This is the atomic core: `auth.py` rewritten over sessions, the conftest fixture reimplemented behind its unchanged interface, `peek_principal` reworked, and every HMAC remnant deleted — in one task, because the suite cannot be green with half a swap.

**Files:**
- Rewrite: `src/marketplace/auth.py`
- Modify: `src/marketplace/idempotency.py:45-70` (principal resolution moves inside the DB session)
- Modify: `src/marketplace/settings.py` (delete `marketplace_secret`, `token_ttl_hours`)
- Modify: `tests/conftest.py` (fixture internals; interface unchanged)
- Modify: `tests/test_auth_and_hardening.py` (ONLY `test_expired_token_rejected` → session equivalent)

**Interfaces:**
- Consumes: `User`, `AuthSession` (Task 2), `settings.session_ttl_hours` (Task 1).
- Produces (Tasks 4-5 rely on): `auth.hash_password(password: str) -> str`, `auth.verify_password(password: str, password_hash: str) -> bool`, `auth._hash_token(raw: str) -> str` (sha256 hex), `auth.create_session(db: Session, user: User) -> tuple[str, datetime]` (raw token + expiry), `auth.Claims` (frozen dataclass: `role: UserRole`, `sub: str`), `auth.Principal` (Annotated dependency), `auth.peek_principal(db: Session, authorization: str | None) -> str | None`, `auth._DUMMY_HASH`, and the unchanged `require_admin`/`current_buyer`/`current_seller`.
- conftest produces: `auth(role, sub)` fixture (unchanged interface, idempotent per (role, sub)), module constant `TEST_PASSWORD = "test-password-123"` and `_TEST_PASSWORD_HASH`.

- [ ] **Step 1: Rewrite `src/marketplace/auth.py`**

Replace the whole file:

```python
"""Session-backed authentication.

Login (the auth endpoints, added alongside) stores a session row keyed by the
sha256 of an opaque bearer token; the dependencies below resolve that token to
``(role, sub=user_id)`` with one indexed lookup. Endpoints derive
``buyer_id``/``seller_id`` from the authenticated principal — never a request
body — so identity cannot be spoofed. Sessions are revocable: logout, bans,
and password resets delete rows.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_session
from .entities import AuthSession, User
from .models import UserRole
from .settings import settings

_password_hash = PasswordHash.recommended()  # argon2id


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


# Verified against when login hits an unknown email, so response timing does
# not reveal whether an account exists.
_DUMMY_HASH = _password_hash.hash("timing-equalizer")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def create_session(db: Session, user: User) -> tuple[str, datetime]:
    """Issue an opaque bearer for ``user``; only its sha256 is stored."""
    raw = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(hours=settings.session_ttl_hours)
    db.add(AuthSession(user_id=user.id, token_hash=_hash_token(raw), expires_at=expires_at))
    db.flush()
    return raw, expires_at


@dataclass(frozen=True)
class Claims:
    role: UserRole
    sub: str  # user id


def _resolve_bearer(db: Session, authorization: str) -> Claims | None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    user = db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(AuthSession.token_hash == _hash_token(token), AuthSession.expires_at > _now())
    )
    if user is None:
        return None
    return Claims(role=user.role, sub=user.id)


_SessionDep = Annotated[Session, Depends(get_session)]


def _principal(session: _SessionDep, authorization: Annotated[str, Header()] = "") -> Claims:
    claims = _resolve_bearer(session, authorization)
    if claims is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return claims


Principal = Annotated[Claims, Depends(_principal)]


def require_admin(claims: Principal) -> str:
    if claims.role is not UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin credentials required")
    return claims.sub


def current_buyer(claims: Principal) -> str:
    if claims.role is not UserRole.BUYER:
        raise HTTPException(status_code=403, detail="buyer credentials required")
    return claims.sub


def current_seller(claims: Principal) -> str:
    if claims.role is not UserRole.SELLER:
        raise HTTPException(status_code=403, detail="seller credentials required")
    return claims.sub


def peek_principal(db: Session, authorization: str | None) -> str | None:
    """Best-effort principal ("role:sub") for middleware. None when absent or
    invalid — the strict endpoint dependencies still produce the real 401."""
    if not authorization:
        return None
    claims = _resolve_bearer(db, authorization)
    return None if claims is None else f"{claims.role}:{claims.sub}"
```

Notes: `get_session` is cached per request by FastAPI, so `_principal` shares the endpoint's DB session — no extra connection. The old `Role` Literal, `mint_token`, `_verify`, `_Claims`, `_sign`, `_b64`, `_unb64` are all gone.

- [ ] **Step 2: Rework `src/marketplace/idempotency.py`**

`peek_principal` now needs a DB session. Replace lines 45-70 (from `principal = peek_principal(...)` through the replay block) so both lookups share one short session that closes before downstream runs:

```python
        path = str(scope["path"])
        with SessionLocal() as session:
            principal = peek_principal(session, headers.get("authorization"))
            row = (
                None
                if principal is None
                else session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.principal == principal, IdempotencyRecord.key == key
                    )
                )
            )
        if principal is None:
            await self.app(scope, receive, send)  # auth 401s downstream with the real error
            return
        if row is not None:
            if row.path != path:
                replay: Response = JSONResponse(
                    {"detail": "Idempotency-Key was already used for a different request"},
                    status_code=409,
                )
            else:
                replay = Response(
                    content=row.response_body,
                    status_code=row.response_status,
                    media_type="application/json",
                )
            await replay(scope, receive, send)
            return
```

(Reading `row.response_body`/`row.response_status`/`row.path` after the session closes is safe: `SessionLocal` is configured with `expire_on_commit=False` and no commit happens in this block — but to be robust, capture the three scalars into locals inside the `with` block if pyright or a detached-instance error complains.)

- [ ] **Step 3: Delete the dead settings**

In `src/marketplace/settings.py` remove the `marketplace_secret` line (and its comment) and `token_ttl_hours`.

- [ ] **Step 4: Rewrite the conftest fixtures**

In `tests/conftest.py`: remove the `os.environ.setdefault("MARKETPLACE_SECRET", ...)` line and the `mint_token` import. Extend imports (with usage, same edit): `from datetime import UTC, datetime, timedelta`, `from marketplace.auth import _hash_token, hash_password`, `from marketplace.entities import AuthSession, Base, SellerProfile, User`, `from marketplace.models import UserRole`.

Replace the `auth` fixture (and add the constants above the fixtures):

```python
TEST_PASSWORD = "test-password-123"
_TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)  # hash once; argon2 is deliberately slow


@pytest.fixture
def auth() -> AuthFactory:
    """Bearer-header factory with the historical interface: auth(role, sub).

    White-box: inserts a User (id == sub, so identity assertions in older
    tests keep working) plus an AuthSession row. Idempotent per (role, sub)
    within a test — repeated calls return the same header."""
    issued: dict[tuple[str, str], Header] = {}

    def _make(role: str, sub: str) -> Header:
        key = (role, sub)
        if key in issued:
            return issued[key]
        raw = f"test-token-{role}-{sub}"
        with SessionLocal() as s:
            if s.get(User, sub) is None:
                s.add(
                    User(
                        id=sub,
                        email=f"{sub}@{role}.test.local",
                        role=UserRole(role),
                        password_hash=_TEST_PASSWORD_HASH,
                        display_name=sub,
                    )
                )
            s.add(
                AuthSession(
                    user_id=sub,
                    token_hash=_hash_token(raw),
                    expires_at=datetime.now(UTC) + timedelta(hours=12),
                )
            )
            s.commit()
        issued[key] = {"Authorization": f"Bearer {raw}"}
        return issued[key]

    return _make
```

The `admin` fixture stays exactly as-is (`return auth("admin", "ops")` works — the fixture creates an admin-role user directly; signup restrictions don't apply to white-box inserts).

- [ ] **Step 5: Replace the one HMAC-specific test**

In `tests/test_auth_and_hardening.py`, replace `test_expired_token_rejected` (currently minting a negative-TTL token) with:

```python
def test_expired_session_rejected(client: TestClient) -> None:
    raw = "expired-session-token"
    with SessionLocal() as s:
        s.add(
            User(
                id="expired-user",
                email="expired@buyer.test.local",
                role=UserRole.BUYER,
                password_hash="irrelevant",
                display_name="expired",
            )
        )
        s.add(
            AuthSession(
                user_id="expired-user",
                token_hash=_hash_token(raw),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        s.commit()
    header = {"Authorization": f"Bearer {raw}"}
    assert (
        client.post("/v1/quotes", json={"service_type_id": "x"}, headers=header).status_code == 401
    )
```

Update that file's imports in the same edit: drop `from marketplace.auth import mint_token`; add `from datetime import UTC, datetime, timedelta`, `from marketplace.auth import _hash_token`, `from marketplace.db import SessionLocal`, `from marketplace.entities import AuthSession, User`, `from marketplace.models import UserRole` (keep the existing imports the file still uses).

- [ ] **Step 6: Hunt stragglers**

Run: `grep -rn "mint_token\|marketplace_secret\|MARKETPLACE_SECRET\|token_ttl_hours" src/ tests/ scripts/ .env.example`
Expected hits ONLY in `scripts/demo.py` and `.env.example` (both are Task 6's to fix — leave them; the demo is not part of the pytest gate). Anything else found must be fixed now.

- [ ] **Step 7: Full gate — the moment of truth**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
Expected: **everything green with zero edits to any test file other than the two named above.** A failure here means an existing test asserts something identity-shaped this plan missed — fix the fixture (not the test) unless the test literally exercises HMAC mechanics.

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/auth.py src/marketplace/idempotency.py src/marketplace/settings.py tests/conftest.py tests/test_auth_and_hardening.py
git commit -m "Swap the trust path: DB-backed sessions replace pilot HMAC tokens

mint_token, HMAC verify, and MARKETPLACE_SECRET are deleted. The
principal seam kept its signatures, so no endpoint changed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Auth endpoints — signup, login, logout, me + admin bootstrap

**Files:**
- Modify: `src/marketplace/models.py` (auth DTOs)
- Modify: `src/marketplace/auth.py` (auth_router + endpoints + bootstrap)
- Modify: `src/marketplace/api.py` (lifespan bootstrap call + router registration)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: Task 3's `hash_password`/`verify_password`/`create_session`/`_DUMMY_HASH`/`Principal`/`_SessionDep`; `repo.get_or_create_buyer`/`get_or_create_seller` (exist).
- Produces: `POST /v1/auth/signup` (201, SessionOut), `POST /v1/auth/login` (SessionOut), `POST /v1/auth/logout`, `GET /v1/auth/me` (UserOut); `auth.bootstrap_admin() -> None`; `auth.auth_router`; models `UserOut{id, email, role, display_name, email_verified}`, `SessionOut{token, expires_at, user: UserOut}`, `SignupRequest`, `LoginRequest`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py` (extend the top imports in the same edit: `from fastapi.testclient import TestClient`, `from tests.conftest import TEST_PASSWORD, AuthFactory`):

```python
def _signup(client: TestClient, role: str, email: str = "kim@example.test") -> dict[str, object]:
    r = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": TEST_PASSWORD, "role": role, "display_name": "Kim"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_signup_returns_working_session(client: TestClient) -> None:
    body = _signup(client, "buyer")
    assert body["user"]["role"] == "buyer"
    assert body["user"]["email_verified"] is False
    header = {"Authorization": f"Bearer {body['token']}"}
    me = client.get("/v1/auth/me", headers=header).json()
    assert me["email"] == "kim@example.test"
    # And the session actually works against a domain endpoint:
    assert client.post("/v1/quotes", json={"service_type_id": "x"}, headers=header).status_code == 404  # unknown service, but authenticated


def test_signup_duplicate_email_role_conflicts(client: TestClient) -> None:
    _signup(client, "buyer")
    r = client.post(
        "/v1/auth/signup",
        json={
            "email": "kim@example.test",
            "password": TEST_PASSWORD,
            "role": "buyer",
            "display_name": "Kim2",
        },
    )
    assert r.status_code == 409


def test_same_email_may_register_both_roles(client: TestClient) -> None:
    buyer = _signup(client, "buyer")
    seller = _signup(client, "seller")
    assert buyer["user"]["id"] != seller["user"]["id"]


def test_signup_cannot_create_admin(client: TestClient) -> None:
    r = client.post(
        "/v1/auth/signup",
        json={
            "email": "evil@example.test",
            "password": TEST_PASSWORD,
            "role": "admin",
            "display_name": "Evil",
        },
    )
    assert r.status_code == 422  # role is Literal[buyer, seller]


def test_login_and_wrong_password(client: TestClient) -> None:
    _signup(client, "buyer")
    ok = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": "wrong-password", "role": "buyer"},
    )
    unknown = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    # Uniform 401: wrong password and unknown account are indistinguishable.
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()


def test_logout_revokes_the_session(client: TestClient) -> None:
    body = _signup(client, "buyer")
    header = {"Authorization": f"Bearer {body['token']}"}
    assert client.post("/v1/auth/logout", headers=header).status_code == 200
    assert client.get("/v1/auth/me", headers=header).status_code == 401


def test_admin_bootstrap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from marketplace import auth as auth_module
    from marketplace.settings import settings as app_settings

    monkeypatch.setattr(app_settings, "admin_email", "root@example.test")
    monkeypatch.setattr(app_settings, "admin_password", "bootstrap-password-1")
    auth_module.bootstrap_admin()
    auth_module.bootstrap_admin()  # idempotent — second run must not raise or duplicate
    r = client.post(
        "/v1/auth/login",
        json={"email": "root@example.test", "password": "bootstrap-password-1", "role": "admin"},
    )
    assert r.status_code == 200
    header = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/v1/admin/transactions", headers=header).status_code == 200
```

(Add `import pytest` to the file's imports if not present.)

Run: `uv run pytest tests/test_auth.py -q`
Expected: new tests FAIL with 404s (`/v1/auth/*` doesn't exist).

- [ ] **Step 2: Add DTOs to `src/marketplace/models.py`**

Extend the pydantic import line with `EmailStr` and the typing import with `Literal` (both in the same edit as the DTOs). After the response-views section:

```python
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: UserRole
    display_name: str
    email_verified: bool


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut
```

In the request-bodies section:

```python
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Literal[UserRole.BUYER, UserRole.SELLER]  # admin is seeded, never signup
    display_name: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    role: UserRole  # the same email may own one account per role
```

- [ ] **Step 3: Add the router + bootstrap to `src/marketplace/auth.py`**

Extend imports (with usage): `from fastapi import APIRouter`, `from .db import SessionLocal`, `from . import repo`, `from .models import LoginRequest, SessionOut, SignupRequest, UserOut`, `import logging`.

```python
logger = logging.getLogger("marketplace.auth")

auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _session_out(db: Session, user: User) -> SessionOut:
    token, expires_at = create_session(db, user)
    return SessionOut(token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@auth_router.post("/signup", response_model=SessionOut, status_code=201)
def signup(body: SignupRequest, db: _SessionDep) -> SessionOut:
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email, User.role == body.role)) is not None:
        raise HTTPException(status_code=409, detail="an account with this email and role exists")
    user = User(
        email=email,
        role=UserRole(body.role),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    db.flush()
    # The domain record exists from the first moment the identity does.
    if user.role is UserRole.BUYER:
        repo.get_or_create_buyer(db, user.id)
    else:
        repo.get_or_create_seller(db, user.id)
    return _session_out(db, user)


@auth_router.post("/login", response_model=SessionOut)
def login(body: LoginRequest, db: _SessionDep) -> SessionOut:
    user = db.scalar(
        select(User).where(User.email == body.email.lower(), User.role == body.role)
    )
    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # equalize timing; no enumeration
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return _session_out(db, user)


@auth_router.post("/logout")
def logout(
    db: _SessionDep, authorization: Annotated[str, Header()] = ""
) -> dict[str, str]:
    _, _, token = authorization.partition(" ")
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    if row is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    db.delete(row)
    return {"status": "logged out"}


@auth_router.get("/me", response_model=UserOut)
def me(claims: Principal, db: _SessionDep) -> User:
    user = db.get(User, claims.sub)
    if user is None:  # session outlived the user row (deleted account)
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


def bootstrap_admin() -> None:
    """Seed the admin account from settings at startup. Empty settings -> no
    admin (logged), which is fine for tests and the bare template."""
    if not (settings.admin_email and settings.admin_password):
        logger.info("ADMIN_EMAIL/ADMIN_PASSWORD unset; no admin account seeded")
        return
    email = settings.admin_email.lower()
    with SessionLocal() as db:
        exists = db.scalar(select(User).where(User.email == email, User.role == UserRole.ADMIN))
        if exists is None:
            db.add(
                User(
                    email=email,
                    role=UserRole.ADMIN,
                    password_hash=hash_password(settings.admin_password),
                    display_name="admin",
                )
            )
            db.commit()
            logger.info("admin account seeded for %s", email)
```

- [ ] **Step 4: Wire into `src/marketplace/api.py`**

In `_lifespan` (line ~963), after the `init_db()` block add `bootstrap_admin()` (import `bootstrap_admin` and `auth_router` by extending the existing `from .auth import ...` line, same edit). At the router-registration block add:

```python
app.include_router(auth_router)
```

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/test_auth.py -q` → all pass.
Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → green.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/models.py src/marketplace/auth.py src/marketplace/api.py tests/test_auth.py
git commit -m "Add signup, login, logout, me endpoints and admin bootstrap

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Email flows — verification, password reset, auth sweep

**Files:**
- Modify: `src/marketplace/models.py` (three small DTOs)
- Modify: `src/marketplace/auth.py` (token issuance + verify/reset endpoints)
- Modify: `src/marketplace/api.py` (`_sweep` gains the auth rule)
- Modify: `tests/conftest.py` (mail_outbox fixture)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `EmailToken`/`EmailTokenPurpose` (Task 2), `get_mail_sender`/`use_sender`/`RecordingEmailSender` (Task 1), Task 3-4 helpers.
- Produces: `POST /v1/auth/verify`, `POST /v1/auth/password-reset/request` (always 200), `POST /v1/auth/password-reset/confirm` (revokes all sessions); `api._sweep_expired_auth(session)`; conftest fixture `mail_outbox: RecordingEmailSender`; signup now sends a verification email.

- [ ] **Step 1: Add the conftest fixture**

In `tests/conftest.py` (imports with usage: `from marketplace.mail import RecordingEmailSender, use_sender`):

```python
@pytest.fixture
def mail_outbox() -> Iterator[RecordingEmailSender]:
    """Capture outbound mail via the port (tests read tokens here, not logs)."""
    recorder = RecordingEmailSender()
    previous = use_sender(recorder)
    yield recorder
    use_sender(previous)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_auth.py` (imports with usage: `from marketplace.mail import RecordingEmailSender`):

```python
def _extract_token(outbox: RecordingEmailSender, index: int = -1) -> str:
    # Mail bodies end with "token=<raw>" — the port carries it verbatim.
    body = outbox.sent[index][2]
    return body.rsplit("token=", 1)[1].strip()


def test_signup_sends_verification_and_verify_flips_flag(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    body = _signup(client, "buyer")
    assert len(mail_outbox.sent) == 1
    assert mail_outbox.sent[0][0] == "kim@example.test"
    token = _extract_token(mail_outbox)

    assert client.post("/v1/auth/verify", json={"token": token}).status_code == 200
    header = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/v1/auth/me", headers=header).json()["email_verified"] is True
    # Single-use: replay fails.
    assert client.post("/v1/auth/verify", json={"token": token}).status_code == 400


def test_password_reset_flow_revokes_sessions(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    body = _signup(client, "buyer")
    old_header = {"Authorization": f"Bearer {body['token']}"}
    r = client.post(
        "/v1/auth/password-reset/request", json={"email": "kim@example.test", "role": "buyer"}
    )
    assert r.status_code == 200
    token = _extract_token(mail_outbox)

    r = client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "brand-new-password-1"},
    )
    assert r.status_code == 200
    # Every pre-reset session is dead.
    assert client.get("/v1/auth/me", headers=old_header).status_code == 401
    # The new password works; the old one does not.
    ok = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": "brand-new-password-1", "role": "buyer"},
    )
    assert ok.status_code == 200
    stale = client.post(
        "/v1/auth/login",
        json={"email": "kim@example.test", "password": TEST_PASSWORD, "role": "buyer"},
    )
    assert stale.status_code == 401


def test_reset_request_never_confirms_account_existence(
    client: TestClient, mail_outbox: RecordingEmailSender
) -> None:
    r = client.post(
        "/v1/auth/password-reset/request", json={"email": "ghost@example.test", "role": "buyer"}
    )
    assert r.status_code == 200  # identical to the exists case
    assert mail_outbox.sent == []  # but nothing was sent


def test_sweep_deletes_expired_sessions(client: TestClient, auth: AuthFactory) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from marketplace.db import SessionLocal
    from marketplace.entities import AuthSession

    auth("buyer", "sweepy")  # its session becomes the expired one
    with SessionLocal() as s:
        row = s.scalar(select(AuthSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        s.commit()
    # An EXPIRED principal 401s during dependency resolution — the endpoint
    # body (and its sweep) never runs. A different, live principal must
    # trigger maintenance.
    live = auth("seller", "sweeper")
    client.get("/v1/seller/offers", headers=live)  # list_offers calls _sweep
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AuthSession)) == 1  # only the live one
```

Run: `uv run pytest tests/test_auth.py -q`
Expected: the 4 new tests FAIL (404 on the new endpoints; signup sends nothing; sweep keeps the row).

- [ ] **Step 3: Add DTOs to `src/marketplace/models.py`**

```python
class VerifyRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ResetRequest(BaseModel):
    email: EmailStr
    role: Literal[UserRole.BUYER, UserRole.SELLER]


class ResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)
```

- [ ] **Step 4: Token issuance + endpoints in `src/marketplace/auth.py`**

Imports (with usage): `from sqlalchemy import delete, select` (extend the existing select import), `from .entities import AuthSession, EmailToken, User`, `from .mail import EmailSender, get_mail_sender`, `from .models import EmailTokenPurpose, ResetConfirmRequest, ResetRequest, VerifyRequest` (extend existing lines).

```python
_VERIFY_TTL_HOURS = 48
_RESET_TTL_HOURS = 1

MailDep = Annotated[EmailSender, Depends(get_mail_sender)]


def _issue_email_token(
    db: Session, mail: EmailSender, user: User, purpose: EmailTokenPurpose
) -> None:
    raw = secrets.token_urlsafe(32)
    ttl = _VERIFY_TTL_HOURS if purpose is EmailTokenPurpose.VERIFY else _RESET_TTL_HOURS
    db.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash_token(raw),
            expires_at=_now() + timedelta(hours=ttl),
        )
    )
    action = "verify" if purpose is EmailTokenPurpose.VERIFY else "password-reset/confirm"
    mail.send(
        user.email,
        "Verify your email" if purpose is EmailTokenPurpose.VERIFY else "Reset your password",
        f"Visit {settings.base_url}/{action}?token={raw}",
    )
```

In `signup`, after the profile-row creation and before `return _session_out(...)`, add the parameter `mail: MailDep` to the signature and the call:

```python
    _issue_email_token(db, mail, user, EmailTokenPurpose.VERIFY)
```

New endpoints:

```python
@auth_router.post("/verify")
def verify_email(body: VerifyRequest, db: _SessionDep) -> dict[str, str]:
    row = db.scalar(
        select(EmailToken)
        .where(EmailToken.token_hash == _hash_token(body.token))
        .with_for_update()
    )
    if (
        row is None
        or row.used_at is not None
        or row.expires_at < _now()
        or row.purpose is not EmailTokenPurpose.VERIFY
    ):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user.email_verified = True
    row.used_at = _now()
    return {"status": "verified"}


@auth_router.post("/password-reset/request")
def request_password_reset(body: ResetRequest, db: _SessionDep, mail: MailDep) -> dict[str, str]:
    user = db.scalar(
        select(User).where(User.email == body.email.lower(), User.role == body.role)
    )
    if user is not None:
        _issue_email_token(db, mail, user, EmailTokenPurpose.RESET)
    return {"status": "ok"}  # identical either way: no account enumeration


@auth_router.post("/password-reset/confirm")
def confirm_password_reset(body: ResetConfirmRequest, db: _SessionDep) -> dict[str, str]:
    row = db.scalar(
        select(EmailToken)
        .where(EmailToken.token_hash == _hash_token(body.token))
        .with_for_update()
    )
    if (
        row is None
        or row.used_at is not None
        or row.expires_at < _now()
        or row.purpose is not EmailTokenPurpose.RESET
    ):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user.password_hash = hash_password(body.new_password)
    row.used_at = _now()
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))  # revoke everything
    return {"status": "password reset"}
```

- [ ] **Step 5: The sweep rule in `src/marketplace/api.py`**

Extend the entities import with `AuthSession, EmailToken` (same edit as usage). Below `_sweep_stale_payments`:

```python
def _sweep_expired_auth(session: Session) -> None:
    """Expired sessions and email tokens are dead weight — drop them on reads."""
    session.execute(delete(AuthSession).where(AuthSession.expires_at < _now()))
    session.execute(delete(EmailToken).where(EmailToken.expires_at < _now()))
```

And `_sweep` becomes:

```python
def _sweep(session: Session, provider: PaymentProvider) -> None:
    """Everything lazy maintenance does on reads: offers, payments, auth."""
    _sweep_expired_offers(session)
    _sweep_stale_payments(session, provider)
    _sweep_expired_auth(session)
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/test_auth.py -q` → all pass (earlier auth tests too — signup now sends mail; the console adapter handles tests without `mail_outbox`).
Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q` → green.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/models.py src/marketplace/auth.py src/marketplace/api.py tests/conftest.py tests/test_auth.py
git commit -m "Add email verification and password reset over the mail port

Reset revokes every session; reset requests never confirm account
existence; expired sessions and tokens fall to the lazy sweep.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Demo, docs, env example, final verification

**Files:**
- Modify: `scripts/demo.py`, `.env.example`, `README.md`, `CLAUDE.md`, `SECURITY.md`, `ROADMAP.md`

**Interfaces:** none produced — documentation and demo of everything above.

- [ ] **Step 1: Update `scripts/demo.py`**

Read the script first; keep its structure and print style. Changes:
1. Before the app import, seed admin credentials so the lifespan bootstraps an admin:

```python
os.environ.setdefault("ADMIN_EMAIL", "admin@demo.local")
os.environ.setdefault("ADMIN_PASSWORD", "demo-admin-password")
```

2. Replace every `mint_token(...)`-based header with real flows:
   - admin: `POST /v1/auth/login` with the credentials above,
   - buyer/seller: `POST /v1/auth/signup` (`buyer@demo.local` / `seller@demo.local`, password `demo-password-1`, roles buyer/seller), using the returned `token`.
3. Keep both payment acts; the seller/buyer ids inside the flow now come from `signup_response["user"]["id"]` where the script needs them.
4. End-of-script asserts: add one that `GET /v1/auth/me` returns the signed-up buyer email.

Run: `uv run python scripts/demo.py`
Expected: exit 0, both acts print, no HMAC references remain (`grep -c mint_token scripts/demo.py` → 0).

- [ ] **Step 2: Update `.env.example`**

Remove the `MARKETPLACE_SECRET` line. Append:

```bash
# Auth — sessions are DB-backed; the admin account is seeded at startup.
# ADMIN_EMAIL=ops@your-app.example
# ADMIN_PASSWORD=change-me
# SESSION_TTL_HOURS=72
# BASE_URL=https://your-app.example
```

- [ ] **Step 3: Update the docs**

- `README.md`: auth section — signup/login/logout/me/verify/password-reset endpoint map, separate accounts per role, admin seeding via env, console mail adapter (links land in logs until a real sender is plugged in).
- `CLAUDE.md`: Non-negotiables — replace the pilot-HMAC bullet: identity resolves through `auth_sessions` rows only; never reintroduce token minting or a second verifier; passwords only as argon2 hashes via `auth.hash_password`; email/reset tokens stored sha256-only and single-use. Subtle bits — the conftest `auth` fixture white-box-inserts users with `id == sub` (why User.id is String(128)); `mail.use_sender` is the test seam.
- `SECURITY.md`: rewrite the threat-model section — sessions replace HMAC (revocable, hash-at-rest); uniform login 401s + dummy-hash timing equalization; reset revokes all sessions; no-enumeration reset; residuals: no login rate limiting (gateway item), verification gates nothing until a real mail adapter exists.
- `ROADMAP.md`: move Auth into Done ✓ (with residuals named); "What's still ahead" reorders — notifications (now unblocked: identities have emails + the mail port exists) leads; add OAuth/social login as a fork-time item.

- [ ] **Step 4: Final verification (exit codes visible, never piped)**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
uv run python scripts/demo.py
rm -f /tmp/claude-1000/mig.db && DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
grep -rn "mint_token\|marketplace_secret\|MARKETPLACE_SECRET" src/ tests/ scripts/ .env.example || echo "HMAC fully gone"
```

Expected: gate green; demo exit 0; three migrations apply from scratch; the grep prints "HMAC fully gone".

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py .env.example README.md CLAUDE.md SECURITY.md ROADMAP.md
git commit -m "Document real-user auth: demo signup flows, env, security model

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review checklist (run after writing, fixed inline)

- **Spec coverage:** separate per-role accounts + unique(email, role) (T2/T4) · DB sessions, sha256 at rest, revocation (T3/T4) · argon2/pwdlib (T1/T3) · EmailSender port + console adapter (T1) · verify + reset + always-200 + revoke-all (T5) · admin bootstrap + no-admin-signup (T4) · HMAC deletion incl. MARKETPLACE_SECRET + middleware peek over sessions (T3) · conftest fixture unchanged interface, idempotent, shared hash, id == sub (T3) · sweep rule (T5) · migration #3 (T2) · demo/docs/env (T6) · pilot-grade postures documented not built (T6 SECURITY.md).
- **Type consistency:** `Claims(role: UserRole, sub: str)` used by deps and peek; `create_session -> tuple[str, datetime]` consumed by `_session_out`; `_hash_token` shared by fixtures, logout, verify, reset; `TEST_PASSWORD` exported from conftest and imported by test_auth (public name, no underscore).
- **Known judgment calls, named for the implementer:** `test_signup_returns_working_session` asserts a 404 from `/v1/quotes` to prove authentication (unknown service id) — deliberate: 401 vs 404 distinguishes auth from domain. The sweep test deliberately uses a second live principal as the trigger, because an expired principal 401s during dependency resolution and the endpoint body (with its sweep) never runs.
- **Placeholders:** none — every step carries runnable code or exact commands.
