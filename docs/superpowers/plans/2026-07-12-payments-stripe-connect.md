# Payments & Payouts (Stripe Connect) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real money movement for the marketplace template — escrow (charge buyer at accept, transfer seller payout at complete), refunds, seller onboarding, webhooks, and client idempotency keys — runnable with zero Stripe credentials via a deterministic fake provider.

**Architecture:** A `PaymentProvider` protocol in `src/marketplace/payments/` with two adapters (`FakeProvider` for dev/tests, `StripeProvider` for production). The job state machine gains `AWAITING_PAYMENT` between seller-accept and `ACCEPTED`. New `Payment`/`Payout` rows record cash movement (the existing `Transaction` stays the margin ledger). A webhook endpoint applies provider events with dedup; an ASGI middleware implements client `Idempotency-Key` replay.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + Alembic, Pydantic v2, `stripe` SDK (new dep, only loaded when configured), pytest on SQLite (Postgres via `DATABASE_URL`).

**Spec:** `docs/superpowers/specs/2026-07-12-payments-stripe-connect-design.md` (approved). Branch: `payments-stripe-connect`.

## Global Constraints

- Package manager is `uv` — `uv run pytest`, `uv add`, never pip/venv.
- Gate after every task: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` — all green, pyright **strict** (do not drop to basic).
- The PostToolUse formatter deletes not-yet-used imports. Add an import in the same edit as the code that uses it; if an import vanishes, re-add it after the usage lands.
- No backticks inside double-quoted `git commit -m` strings.
- Never `git add -A` / `git add .` — stage exact paths.
- Identity comes from the authenticated principal, never a request body (`auth.py` dependencies).
- ORM entities never leave the API layer — endpoints return entities converted via `response_model` views, or explicit Pydantic views. Never hand-build dicts for responses.
- Money is `Decimal`, quantized via `models.to_money` (2 dp half-up), serialized as JSON strings. Providers get integer minor units via `to_minor_units`.
- The pricing/matching core (`pricing.py`, `matching.py`, `config.py`) stays pure — payments never touch it.
- `logging` module only, no `print()` (exception: `scripts/demo.py` already uses prints by design — follow its existing style).
- Mark deliberate simplifications with a `ponytail:` comment naming the ceiling and upgrade path.
- Tests run on SQLite by default (`tests/conftest.py` sets `DATABASE_URL` before app import); `with_for_update` is a no-op there and real on Postgres — keep using it anyway (codebase convention).

---

### Task 1: Domain enums, settings, payment port, FakeProvider

**Files:**
- Modify: `src/marketplace/models.py` (add `AWAITING_PAYMENT`, `PaymentStatus`, `PayoutStatus`; new views come in later tasks)
- Modify: `src/marketplace/settings.py`
- Create: `src/marketplace/payments/__init__.py`
- Create: `src/marketplace/payments/port.py`
- Create: `src/marketplace/payments/fake.py`
- Test: `tests/test_payments_port.py`

**Interfaces:**
- Consumes: `models.to_money`, `settings.Settings`.
- Produces (later tasks import these exactly):
  - `marketplace.models.PaymentStatus` (`PENDING/SUCCEEDED/FAILED/REFUNDED`), `marketplace.models.PayoutStatus` (`PENDING/PAID/FAILED`), `marketplace.models.JobStatus.AWAITING_PAYMENT`
  - `marketplace.payments.port`: `PaymentError`, `WebhookSignatureError`, `to_minor_units(amount: Decimal) -> int`, dataclasses `AccountResult(provider_account_id: str, payments_ready: bool)`, `ChargeResult(provider_payment_id: str, status: PaymentStatus, client_secret: str | None)`, `TransferResult(provider_transfer_id: str, status: PayoutStatus)`, `RefundResult(provider_refund_id: str)`, `PaymentEvent(event_id: str, kind: str, object_id: str, payments_ready: bool | None = None)`, protocol `PaymentProvider` (methods below)
  - `marketplace.payments`: `fake_provider` (module singleton `FakeProvider`), `get_provider() -> PaymentProvider`
  - `settings`: `stripe_secret_key: str = ""`, `stripe_webhook_secret: str = ""`, `currency: str = "usd"`, `payment_ttl_minutes: int = 30`, `onboarding_return_url: str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_payments_port.py`:

```python
"""Pure unit tests for the payment port and the deterministic fake provider."""

from decimal import Decimal

import pytest

from marketplace.models import PaymentStatus, PayoutStatus, to_money
from marketplace.payments import fake_provider, get_provider
from marketplace.payments.fake import FakeProvider
from marketplace.payments.port import ChargeResult, PaymentError, to_minor_units


def _charge(fake: FakeProvider) -> ChargeResult:
    return fake.charge_buyer(
        buyer_id="alice",
        amount=Decimal("10.00"),
        currency="usd",
        job_id="job-1",
        idempotency_key="charge:job-1",
    )


def test_to_minor_units() -> None:
    assert to_minor_units(to_money("12.34")) == 1234
    assert to_minor_units(to_money(0)) == 0
    assert to_minor_units(to_money("0.05")) == 5
    assert to_minor_units(to_money("19.999")) == 2000  # to_money already rounded half-up


def test_default_provider_is_the_fake_singleton() -> None:
    assert get_provider() is fake_provider


def test_fake_charge_succeeds_instantly_by_default() -> None:
    fake = FakeProvider()
    result = _charge(fake)
    assert result.status is PaymentStatus.SUCCEEDED
    assert result.client_secret is None
    assert result.provider_payment_id.startswith("pay_fake_")


def test_fake_scripted_pending_is_one_shot() -> None:
    fake = FakeProvider()
    fake.next_charge_status = PaymentStatus.PENDING
    first = _charge(fake)
    second = _charge(fake)
    assert first.status is PaymentStatus.PENDING
    assert first.client_secret is not None
    assert second.status is PaymentStatus.SUCCEEDED


def test_fake_outage_raises_once_then_recovers() -> None:
    fake = FakeProvider()
    fake.fail_next_call = True
    with pytest.raises(PaymentError):
        _charge(fake)
    assert _charge(fake).status is PaymentStatus.SUCCEEDED


def test_fake_onboarding_is_instantly_ready() -> None:
    fake = FakeProvider()
    acct = fake.create_seller_account("bob", idempotency_key="acct:bob")
    assert acct.payments_ready is True
    assert "bob" in acct.provider_account_id
    assert acct.provider_account_id in fake.onboarding_link(acct.provider_account_id, "http://x")


def test_fake_transfer_and_refund_and_cancel() -> None:
    fake = FakeProvider()
    tr = fake.transfer_to_seller(
        provider_account_id="acct_fake_bob",
        amount=Decimal("14.00"),
        currency="usd",
        job_id="job-1",
        idempotency_key="transfer:job-1",
    )
    assert tr.status is PayoutStatus.PAID
    fake.next_transfer_status = PayoutStatus.FAILED
    tr2 = fake.transfer_to_seller(
        provider_account_id="acct_fake_bob",
        amount=Decimal("14.00"),
        currency="usd",
        job_id="job-2",
        idempotency_key="transfer:job-2",
    )
    assert tr2.status is PayoutStatus.FAILED
    fake.refund("pay_fake_1", idempotency_key="refund:job-1")
    assert fake.refunded == ["pay_fake_1"]
    fake.cancel_charge("pay_fake_2")
    assert fake.cancelled == ["pay_fake_2"]


def test_fake_parses_unsigned_json_webhooks() -> None:
    fake = FakeProvider()
    event = fake.parse_webhook(
        b'{"event_id": "evt_1", "kind": "payment_succeeded", "object_id": "pay_fake_1"}', None
    )
    assert event.event_id == "evt_1"
    assert event.kind == "payment_succeeded"
    assert event.object_id == "pay_fake_1"
    assert event.payments_ready is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_payments_port.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marketplace.payments'` (and `ImportError` for `PaymentStatus`).

- [ ] **Step 3: Add the domain enums to `src/marketplace/models.py`**

Edit `JobStatus` (currently at line 31) to add the new member with its comment:

```python
class JobStatus(StrEnum):
    PENDING = "pending"  # created, an offer is out (or being (re)matched)
    AWAITING_PAYMENT = "awaiting_payment"  # seller committed; buyer's charge not yet secured
    ACCEPTED = "accepted"  # a seller committed AND the money is secured
    COMPLETED = "completed"
    EXPIRED = "expired"  # no seller took it (or payment never arrived)
    CANCELLED = "cancelled"
```

Below `OfferStatus`, add:

```python
class PaymentStatus(StrEnum):
    PENDING = "pending"  # created; awaiting buyer confirmation / provider settlement
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # includes voided/cancelled charges — ponytail: one bucket, split if ops needs it
    REFUNDED = "refunded"


class PayoutStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"  # transfer rejected/errored; admin retries via /v1/admin/payouts/{id}/retry
```

- [ ] **Step 4: Add payment settings to `src/marketplace/settings.py`**

Inside `Settings`, after `token_ttl_hours`:

```python
    # Payments. STRIPE_SECRET_KEY set → real Stripe adapter; unset → deterministic
    # in-memory fake (dev/tests, no account needed).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "usd"  # ponytail: single currency; multi-currency is a fork concern
    payment_ttl_minutes: int = 30  # AWAITING_PAYMENT older than this expires on sweep
    onboarding_return_url: str = "http://localhost:8000/onboarded"
```

- [ ] **Step 5: Create `src/marketplace/payments/port.py`**

```python
"""Payment provider port — the seam between the marketplace and money movers.

The app only ever talks to this protocol; `fake.py` (dev/tests) and
`stripe_provider.py` (production) implement it. Amounts cross the boundary as
2-dp Decimals and are converted to integer minor units at the provider edge.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..models import PaymentStatus, PayoutStatus


class PaymentError(Exception):
    """Provider call failed (network/provider rejection). Callers may retry."""


class WebhookSignatureError(PaymentError):
    """Webhook payload failed signature verification."""


def to_minor_units(amount: Decimal) -> int:
    """2-dp Decimal → integer minor units (cents). Providers speak integers.

    Expects an already-quantized amount (everything passing through
    `models.to_money` is), so the shift is exact.
    """
    return int(amount.scaleb(2).to_integral_value())


@dataclass(frozen=True)
class AccountResult:
    provider_account_id: str
    payments_ready: bool


@dataclass(frozen=True)
class ChargeResult:
    provider_payment_id: str
    status: PaymentStatus
    client_secret: str | None  # buyer-side confirmation secret while status is PENDING


@dataclass(frozen=True)
class TransferResult:
    provider_transfer_id: str
    status: PayoutStatus


@dataclass(frozen=True)
class RefundResult:
    provider_refund_id: str


@dataclass(frozen=True)
class PaymentEvent:
    """Normalized webhook event. `kind` is one of: payment_succeeded,
    payment_failed, account_updated, transfer_paid, transfer_failed, ignored."""

    event_id: str
    kind: str
    object_id: str  # provider payment/account/transfer id the event refers to
    payments_ready: bool | None = None  # only for account_updated


class PaymentProvider(Protocol):
    name: str

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult: ...

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str: ...

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult: ...

    def cancel_charge(self, provider_payment_id: str) -> None: ...

    def refund(self, provider_payment_id: str, *, idempotency_key: str) -> RefundResult: ...

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult: ...

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent: ...
```

- [ ] **Step 6: Create `src/marketplace/payments/fake.py`**

```python
"""Deterministic in-memory provider for dev and tests.

Charges succeed instantly by default so demos and tests flow without webhooks.
Tests script the async path via one-shot attributes (`next_charge_status`,
`next_transfer_status`) or force an outage with `fail_next_call`. Webhooks are
unsigned JSON — this provider is never selected when STRIPE_SECRET_KEY is set,
so unsigned input is dev-only by construction.
"""

import json
from decimal import Decimal
from itertools import count
from typing import Any

from ..models import PaymentStatus, PayoutStatus
from .port import (
    AccountResult,
    ChargeResult,
    PaymentError,
    PaymentEvent,
    RefundResult,
    TransferResult,
)


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self._seq = count(1)
        self.next_charge_status: PaymentStatus = PaymentStatus.SUCCEEDED
        self.next_transfer_status: PayoutStatus = PayoutStatus.PAID
        self.fail_next_call: bool = False
        self.cancelled: list[str] = []
        self.refunded: list[str] = []

    def reset(self) -> None:
        self.next_charge_status = PaymentStatus.SUCCEEDED
        self.next_transfer_status = PayoutStatus.PAID
        self.fail_next_call = False
        self.cancelled.clear()
        self.refunded.clear()

    def _maybe_fail(self) -> None:
        if self.fail_next_call:
            self.fail_next_call = False
            raise PaymentError("fake provider outage (scripted)")

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult:
        self._maybe_fail()
        return AccountResult(provider_account_id=f"acct_fake_{seller_id}", payments_ready=True)

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str:
        return f"https://fake.example/onboard/{provider_account_id}?return={return_url}"

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult:
        self._maybe_fail()
        status = self.next_charge_status
        self.next_charge_status = PaymentStatus.SUCCEEDED  # scripted statuses are one-shot
        n = next(self._seq)
        return ChargeResult(
            provider_payment_id=f"pay_fake_{n}",
            status=status,
            client_secret=None if status is PaymentStatus.SUCCEEDED else f"cs_fake_{n}",
        )

    def cancel_charge(self, provider_payment_id: str) -> None:
        self.cancelled.append(provider_payment_id)

    def refund(self, provider_payment_id: str, *, idempotency_key: str) -> RefundResult:
        self._maybe_fail()
        self.refunded.append(provider_payment_id)
        return RefundResult(provider_refund_id=f"re_fake_{provider_payment_id}")

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult:
        self._maybe_fail()
        status = self.next_transfer_status
        self.next_transfer_status = PayoutStatus.PAID
        return TransferResult(provider_transfer_id=f"tr_fake_{next(self._seq)}", status=status)

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent:
        data: dict[str, Any] = json.loads(payload)
        ready = data.get("payments_ready")
        return PaymentEvent(
            event_id=str(data["event_id"]),
            kind=str(data["kind"]),
            object_id=str(data["object_id"]),
            payments_ready=None if ready is None else bool(ready),
        )
```

- [ ] **Step 7: Create `src/marketplace/payments/__init__.py`**

```python
"""Provider selection. STRIPE_SECRET_KEY set → Stripe (added in a later task);
unset → the deterministic fake. The fake is a module singleton so scripted test
state and the app see the same instance."""

from .fake import FakeProvider
from .port import PaymentProvider

fake_provider = FakeProvider()


def get_provider() -> PaymentProvider:
    return fake_provider
```

(`PaymentProvider` is imported for the return annotation — the Stripe branch replaces this body in Task 9.)

- [ ] **Step 8: Run tests, lint, types**

Run: `uv run pytest tests/test_payments_port.py -q && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: all pass, 0 pyright errors. If ruff flags the `_charge` helper's missing return annotation, annotate it `-> ChargeResult` and import `ChargeResult` from `marketplace.payments.port` instead of using `noqa`.

- [ ] **Step 9: Run the full suite (nothing existing should break)**

Run: `uv run pytest -q`
Expected: previous 54 pass + new tests pass, 1 Postgres skip.

- [ ] **Step 10: Commit**

```bash
git add src/marketplace/models.py src/marketplace/settings.py src/marketplace/payments/ tests/test_payments_port.py
git commit -m "Add payment provider port, fake adapter, and payment domain types

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Payment entities + Alembic migration

**Files:**
- Modify: `src/marketplace/entities.py`
- Create: `migrations/versions/<autogen>_payments.py` (via alembic autogenerate)
- Test: `tests/test_payments_port.py` (append one schema test)

**Interfaces:**
- Consumes: `models.PaymentStatus`, `models.PayoutStatus` (Task 1).
- Produces: entities `Payment`, `Payout`, `WebhookEvent`, `IdempotencyRecord`; `SellerProfile.provider_account_id: Mapped[str | None]`, `SellerProfile.payments_ready: Mapped[bool]` (default `False`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_payments_port.py`:

```python
def test_payment_tables_registered() -> None:
    from marketplace.entities import Base

    assert {"payments", "payouts", "webhook_events", "idempotency_keys"} <= set(
        Base.metadata.tables
    )
```

Run: `uv run pytest tests/test_payments_port.py::test_payment_tables_registered -q`
Expected: FAIL — assertion error (tables missing).

- [ ] **Step 2: Extend `src/marketplace/entities.py`**

Change the import from `.models` (line 31) to include the new enums:

```python
from .models import JobStatus, OfferStatus, PaymentStatus, PayoutStatus
```

Widen `_enum` (line 63) — it currently accepts only Job/Offer status; all four are `StrEnum`:

```python
def _enum(enum_type: type[StrEnum]) -> SAEnum:
    # Store the enum's string values (not names), non-native for SQLite portability.
    return SAEnum(enum_type, native_enum=False, values_callable=_enum_values, length=32)
```

(Add `StrEnum` to the existing `from enum import Enum` import: `from enum import Enum, StrEnum`.)

Add `Text` to the sqlalchemy import block (used by `IdempotencyRecord`).

In `SellerProfile`, after `completed_jobs`:

```python
    provider_account_id: Mapped[str | None] = mapped_column(String(256), default=None)
    payments_ready: Mapped[bool] = mapped_column(default=False)  # set by account webhook
```

After `AuditLog`, add the four new tables:

```python
class Payment(Base):
    """Buyer charge for a job (1:1). Cash record — `Transaction` stays the margin ledger."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[Decimal] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus), default=PaymentStatus.PENDING, index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str] = mapped_column(String(256), index=True)
    client_secret: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class Payout(Base):
    """Seller transfer for a completed job (1:1)."""

    __tablename__ = "payouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    seller_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[Decimal] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[PayoutStatus] = mapped_column(
        _enum(PayoutStatus), default=PayoutStatus.PENDING, index=True
    )
    provider_transfer_id: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(_TS, default=_now, onupdate=_now)


class WebhookEvent(Base):
    """Processed provider events — the dedup ledger (replayed events no-op)."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_event_id: Mapped[str] = mapped_column(String(256), unique=True)
    kind: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(_TS, default=_now)


class IdempotencyRecord(Base):
    """Stored response for a client Idempotency-Key (scoped per principal)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("principal", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal: Mapped[str] = mapped_column(String(150))  # "role:sub"
    key: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(256))
    response_status: Mapped[int] = mapped_column()
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TS, default=_now)
```

- [ ] **Step 3: Run the schema test**

Run: `uv run pytest tests/test_payments_port.py -q`
Expected: PASS.

- [ ] **Step 4: Generate the Alembic migration**

```bash
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic revision --autogenerate -m "payments"
```

Expected: a new file in `migrations/versions/` creating `payments`, `payouts`, `webhook_events`, `idempotency_keys` and adding two `seller_profiles` columns. `UTCDateTime` columns must render as `sa.DateTime(timezone=True)` (the `render_item` hook in `migrations/env.py` does this — verify no `marketplace.` import appears in the migration).

- [ ] **Step 5: Hand-fix the non-null bool column**

Autogen emits `payments_ready` as `nullable=False` with no server default — that fails on any non-empty `seller_profiles` table. Edit the generated migration's `add_column` for `payments_ready` to:

```python
    op.add_column(
        "seller_profiles",
        sa.Column("payments_ready", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
```

(`sa.false()` renders correctly on both Postgres and SQLite. Existing sellers start not-ready — correct: they must onboard.)

- [ ] **Step 6: Verify the migration applies cleanly from scratch**

```bash
rm -f /tmp/claude-1000/mig.db
DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
```

Expected: exits 0, both migrations applied.

- [ ] **Step 7: Full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
Expected: all green. (Ruff excludes `migrations/` via pyproject — the autogen file is not linted.)

- [ ] **Step 8: Commit**

```bash
git add src/marketplace/entities.py migrations/versions/ tests/test_payments_port.py
git commit -m "Add Payment, Payout, WebhookEvent, IdempotencyRecord entities and migration

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Onboarding endpoint + payments_ready gate in matching

This task changes matching eligibility, which breaks every existing test that posts availability — they are all updated here so the suite ends green.

**Files:**
- Modify: `src/marketplace/repo.py:105-138` (`active_job_count`, `active_demand`, `eligible_candidates`)
- Modify: `src/marketplace/models.py` (add `OnboardingOut`)
- Modify: `src/marketplace/api.py` (ProviderDep alias + onboard endpoint)
- Modify (mechanical, add one onboard line before each availability post): `tests/test_auth_and_hardening.py`, `tests/test_end_to_end.py`, `tests/test_information_asymmetry.py`, `tests/test_lifecycle.py`, `tests/test_margin_floor.py`, `tests/test_matching.py`, `tests/test_ratings.py`
- Test: `tests/test_payments.py` (new)

**Interfaces:**
- Consumes: `payments.get_provider`, `port.PaymentProvider`, `AccountResult` (Task 1); `SellerProfile.payments_ready` (Task 2).
- Produces: `POST /v1/seller/payments/onboard` → `OnboardingOut{onboarding_url: str, payments_ready: bool}`; `api.ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]`; `repo.active_job_count` now counts `ACCEPTED` **and** `AWAITING_PAYMENT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_payments.py`:

```python
"""Payment flows against the fake provider: onboarding, gating."""

from fastapi.testclient import TestClient

from tests.conftest import AuthFactory, Header


def onboard_and_avail(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    """The standard seller setup: payment onboarding, then availability."""
    client.post("/v1/seller/payments/onboard", headers=auth("seller", seller))
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )


def new_job(client: TestClient, auth: AuthFactory, sid: str, buyer: str) -> dict[str, object]:
    qid = client.post(
        "/v1/quotes", json={"service_type_id": sid}, headers=auth("buyer", buyer)
    ).json()["id"]
    return client.post("/v1/jobs", json={"quote_id": qid}, headers=auth("buyer", buyer)).json()


def test_onboard_returns_link_and_ready(client: TestClient, auth: AuthFactory) -> None:
    r = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    assert r.status_code == 200
    body = r.json()
    assert body["payments_ready"] is True  # fake is instantly ready
    assert body["onboarding_url"].startswith("https://fake.example/onboard/")


def test_onboard_is_idempotent(client: TestClient, auth: AuthFactory) -> None:
    first = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1")).json()
    second = client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1")).json()
    assert first == second


def test_unonboarded_seller_never_matched(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    # Availability WITHOUT onboarding: the seller can't be paid, so can't be offered work.
    client.post(
        "/v1/seller/availability",
        json={"service_type_id": basic_service},
        headers=auth("seller", "ghost"),
    )
    job = new_job(client, auth, basic_service, "alice")
    assert job["status"] == "expired"  # no eligible seller
    assert client.get("/v1/seller/offers", headers=auth("seller", "ghost")).json() == []


def test_onboarded_seller_is_matched(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    assert job["status"] == "pending"
    assert len(client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()) == 1
```

Run: `uv run pytest tests/test_payments.py -q`
Expected: FAIL — 404 on `/v1/seller/payments/onboard` (`test_unonboarded_seller_never_matched` may already pass once gating lands; the others fail first).

- [ ] **Step 2: Add `OnboardingOut` to `src/marketplace/models.py`**

After `SellerProfileOut`:

```python
class OnboardingOut(BaseModel):
    onboarding_url: str
    payments_ready: bool
```

- [ ] **Step 3: Gate matching in `src/marketplace/repo.py`**

`active_job_count` (line 105) — an `AWAITING_PAYMENT` job holds a capacity slot (the seller committed):

```python
def active_job_count(session: Session, seller_id: str) -> int:
    """Jobs a seller has committed to and not yet completed — their current load.

    AWAITING_PAYMENT counts: the seller accepted; the slot is held while the
    buyer's charge settles."""
    n = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.seller_id == seller_id,
            Job.status.in_([JobStatus.ACCEPTED, JobStatus.AWAITING_PAYMENT]),
        )
    )
    return n or 0
```

`active_demand` (line 92) — payment-pending jobs are still in flight:

```python
            Job.status.in_([JobStatus.PENDING, JobStatus.AWAITING_PAYMENT, JobStatus.ACCEPTED]),
```

`eligible_candidates` (line 115) — inside the loop, right after `prof = get_or_create_seller(...)`:

```python
        prof = get_or_create_seller(session, a.seller_id)
        if not prof.payments_ready:
            continue  # can't be paid → can't be offered work
```

- [ ] **Step 4: Add the onboard endpoint to `src/marketplace/api.py`**

Extend the imports: add `OnboardingOut` to the `.models` import block, and add (imports must land together with the code below — the formatter strips unused ones):

```python
from .payments import get_provider
from .payments.port import PaymentProvider
```

After the `Offset` alias (line 83):

```python
ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]
```

In the seller router section, after `get_profile`:

```python
@seller_router.post("/payments/onboard", response_model=OnboardingOut)
def onboard_payments(
    session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> OnboardingOut:
    """Create the seller's payment account (once) and return the onboarding link.

    `payments_ready` flips via the provider's account webhook (instantly for the
    fake provider); matching only offers jobs to ready sellers."""
    seller = repo.get_or_create_seller(session, seller_id)
    if seller.provider_account_id is None:
        acct = provider.create_seller_account(seller_id, idempotency_key=f"acct:{seller_id}")
        seller.provider_account_id = acct.provider_account_id
        seller.payments_ready = acct.payments_ready
        session.flush()
    return OnboardingOut(
        onboarding_url=provider.onboarding_link(
            seller.provider_account_id, settings.onboarding_return_url
        ),
        payments_ready=seller.payments_ready,
    )
```

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest tests/test_payments.py -q`
Expected: PASS (all 4).

- [ ] **Step 6: Update every existing availability call site to onboard first**

Run: `grep -rn "seller/availability" tests/` — 9 hits across 7 files. For each file-local helper (e.g. `_available` in `tests/test_auth_and_hardening.py:17`), insert the onboard call as the first line, reusing that helper's own variables:

```python
def _available(client: TestClient, auth: AuthFactory, sid: str, seller: str) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", seller))
    client.post(
        "/v1/seller/availability", json={"service_type_id": sid}, headers=auth("seller", seller)
    )
```

For inline availability posts (no helper), insert the same onboard `client.post` immediately before, with that test's seller header expression. `tests/test_ratings.py` has 3 sites — check whether they share a helper; if not, update each.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: everything passes (previous count + 4 new − 0; 1 Postgres skip). Any remaining failure will name a test whose seller never onboarded — fix by the same one-line insertion.

- [ ] **Step 8: Gate + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: clean.

```bash
git add src/marketplace/repo.py src/marketplace/models.py src/marketplace/api.py tests/test_payments.py tests/test_auth_and_hardening.py tests/test_end_to_end.py tests/test_information_asymmetry.py tests/test_lifecycle.py tests/test_margin_floor.py tests/test_matching.py tests/test_ratings.py
git commit -m "Gate matching on payment onboarding; add seller onboard endpoint

Sellers who cannot be paid are never offered work. AWAITING_PAYMENT
holds a capacity slot.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Accept charges the buyer (escrow entry)

**Files:**
- Modify: `src/marketplace/api.py:384-414` (`accept_offer`), `:243-249` (`get_job_buyer`)
- Modify: `src/marketplace/models.py` (`BuyerJobView` payment fields)
- Modify: `tests/conftest.py` (fake-provider fixtures)
- Test: `tests/test_payments.py`

**Interfaces:**
- Consumes: `ProviderDep` (Task 3), `Payment` entity (Task 2), `ChargeResult`/`PaymentError` (Task 1).
- Produces: `api._buyer_view(session: Session, job: Job) -> BuyerJobView`; `BuyerJobView.payment_status: PaymentStatus | None`, `BuyerJobView.client_secret: str | None`; conftest fixture `fake_payments: FakeProvider` (+ autouse reset).

- [ ] **Step 1: Add fake-provider fixtures to `tests/conftest.py`**

After the `admin` fixture (imports go in the existing import block — `FakeProvider` from `marketplace.payments.fake`, `fake_provider` from `marketplace.payments`):

```python
@pytest.fixture(autouse=True)
def _reset_fake_payments() -> Iterator[None]:
    """The fake provider is a module singleton; scripted state must not leak."""
    fake_provider.reset()
    yield
    fake_provider.reset()


@pytest.fixture
def fake_payments() -> FakeProvider:
    """The live fake-provider singleton, for scripting statuses/outages."""
    return fake_provider
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_payments.py` (extend the top import block as needed: `from marketplace.models import PaymentStatus`, `from marketplace.payments.fake import FakeProvider`):

```python
def _accept_first_offer(client: TestClient, seller: Header) -> dict[str, object]:
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]
    r = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r.status_code == 200, r.text
    return r.json()


def test_accept_charges_and_goes_accepted_inline(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    """Fake charges succeed instantly → the job lands straight in ACCEPTED."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accepted = _accept_first_offer(client, auth("seller", "s1"))
    assert accepted["status"] == "accepted"

    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "succeeded"
    assert view["client_secret"] is None  # nothing left for the buyer to confirm


def test_accept_with_async_charge_awaits_payment(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    """A pending charge parks the job in AWAITING_PAYMENT with a client_secret."""
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    accepted = _accept_first_offer(client, auth("seller", "s1"))
    assert accepted["status"] == "awaiting_payment"

    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "awaiting_payment"
    assert view["payment_status"] == "pending"
    assert str(view["client_secret"]).startswith("cs_fake_")


def test_awaiting_payment_holds_the_capacity_slot(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    fake_payments.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, basic_service, "s1")  # capacity 1
    new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))  # awaiting payment
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "expired"  # only seller's slot is held


def test_provider_outage_rolls_accept_back(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    seller = auth("seller", "s1")
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]

    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r.status_code == 502

    # Nothing stuck: the offer is still open and a retry succeeds.
    assert client.get("/v1/seller/offers", headers=seller).json()[0]["id"] == offer["id"]
    r2 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r2.status_code == 200
    assert r2.json()["status"] == "accepted"
```

Run: `uv run pytest tests/test_payments.py -q`
Expected: the 4 new tests FAIL (no charge happens; `payment_status` key missing).

- [ ] **Step 3: Add payment fields to `BuyerJobView` in `src/marketplace/models.py`**

Append to `BuyerJobView`:

```python
    payment_status: PaymentStatus | None = None
    client_secret: str | None = None  # buyer-side confirmation secret, only while awaiting payment
```

(Seller views deliberately get neither — the charge is the buyer's business.)

- [ ] **Step 4: Wire the charge into `accept_offer` in `src/marketplace/api.py`**

Add to the entity import block: `Payment`. Add to the models import block: `PaymentStatus`. Extend the payments import: `from .payments.port import PaymentError, PaymentProvider`.

Replace the tail of `accept_offer` (from `offer.status = OfferStatus.ACCEPTED` through `return job`) and add `provider: ProviderDep` to its signature:

```python
@seller_router.post("/offers/{offer_id}/accept", response_model=SellerJobView)
def accept_offer(
    offer_id: UUID, session: SessionDep, seller_id: SellerId, provider: ProviderDep
) -> Job:
    offer = session.get(Offer, offer_id, with_for_update=True)
    if offer is None or offer.seller_id != seller_id:
        raise HTTPException(status_code=404, detail="offer not found")
    if offer.status != OfferStatus.OFFERED:
        raise HTTPException(status_code=409, detail=f"offer is {offer.status}, not open")
    if offer.expires_at < _now():
        offer.status = OfferStatus.EXPIRED
        offer.responded_at = _now()
        raise HTTPException(status_code=410, detail="offer expired")

    # Lock the seller row so two concurrent accepts can't exceed capacity.
    seller = session.get(SellerProfile, seller_id, with_for_update=True)
    if seller is None:
        seller = repo.get_or_create_seller(session, seller_id)
    if repo.active_job_count(session, seller_id) >= seller.capacity:
        raise HTTPException(status_code=409, detail="at capacity — complete a job first")

    job = session.get(Job, offer.job_id, with_for_update=True)
    if job is None or job.status != JobStatus.PENDING:
        raise HTTPException(status_code=409, detail="job is no longer open")

    # Charge inside the locked region so capacity + payment commit atomically.
    # On PaymentError everything rolls back and the offer stays acceptable; the
    # outbound key means a retry gets the SAME PaymentIntent back — no strays.
    # ponytail: holds a row lock across a network call; fine at template scale,
    # move to a two-phase outbox if provider latency ever hurts.
    try:
        charge = provider.charge_buyer(
            buyer_id=job.buyer_id,
            amount=job.buyer_price,
            currency=settings.currency,
            job_id=str(job.id),
            idempotency_key=f"charge:{job.id}",
        )
    except PaymentError:
        raise HTTPException(
            status_code=502, detail="payment provider unavailable, retry"
        ) from None
    session.add(
        Payment(
            job_id=job.id,
            buyer_id=job.buyer_id,
            amount=job.buyer_price,
            currency=settings.currency,
            status=charge.status,
            provider=provider.name,
            provider_payment_id=charge.provider_payment_id,
            client_secret=charge.client_secret,
        )
    )

    offer.status = OfferStatus.ACCEPTED
    offer.responded_at = _now()
    job.seller_id = seller_id
    job.seller_payout = offer.seller_payout
    job.accepted_at = _now()
    job.status = (
        JobStatus.ACCEPTED
        if charge.status is PaymentStatus.SUCCEEDED
        else JobStatus.AWAITING_PAYMENT
    )
    session.flush()
    return job
```

- [ ] **Step 5: Expose payment state to the buyer**

Above the buyer router, add a helper (import `select` is already there; add `Payment` if the formatter dropped it):

```python
def _buyer_view(session: Session, job: Job) -> BuyerJobView:
    """BuyerJobView plus the buyer's payment state (never the seller's numbers)."""
    view = BuyerJobView.model_validate(job)
    payment = session.scalar(select(Payment).where(Payment.job_id == job.id))
    if payment is not None:
        view.payment_status = payment.status
        if job.status == JobStatus.AWAITING_PAYMENT:
            view.client_secret = payment.client_secret
    return view
```

Change `get_job_buyer` to return the composed view:

```python
@buyer_router.get("/jobs/{job_id}", response_model=BuyerJobView)
def get_job_buyer(job_id: UUID, session: SessionDep, buyer_id: BuyerId) -> BuyerJobView:
    _sweep_expired_offers(session)
    job = session.get(Job, job_id)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _buyer_view(session, job)
```

(List endpoints keep returning bare jobs — `payment_status` stays `None` there; the detail endpoint is the payment poll. ponytail: avoids N+1 on lists.)

- [ ] **Step 6: Run the new tests, then the full suite**

Run: `uv run pytest tests/test_payments.py -q`
Expected: PASS.
Run: `uv run pytest -q`
Expected: all green — existing accept tests still see `"accepted"` because fake charges succeed inline.

- [ ] **Step 7: Gate + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright`

```bash
git add src/marketplace/api.py src/marketplace/models.py tests/conftest.py tests/test_payments.py
git commit -m "Charge the buyer at accept; add AWAITING_PAYMENT flow

Escrow entry point: charge inside the capacity lock, instant-success
providers land in ACCEPTED, async ones park in AWAITING_PAYMENT with
a client_secret on the buyer's job view.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Webhook endpoint, event dispatch, payment-timeout sweep

**Files:**
- Modify: `src/marketplace/api.py` (payments router, `_apply_payment_event`, `_sweep`, call sites `get_job_buyer` / `list_offers` / admin `sweep`)
- Test: `tests/test_payments.py`

**Interfaces:**
- Consumes: `WebhookEvent`, `Payment`, `Payout` entities; `PaymentEvent`, `WebhookSignatureError` from the port; `provider.cancel_charge`.
- Produces: `POST /v1/payments/webhook` (unauthenticated; returns `{"status": "ok" | "duplicate"}`); `api._sweep(session: Session, provider: PaymentProvider) -> None` (offers + stale payments — later tasks call this, not `_sweep_expired_offers`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payments.py` (add imports with the code: `from datetime import UTC, datetime, timedelta`, `from uuid import UUID`, `from sqlalchemy import select`, `from marketplace.db import SessionLocal`, `from marketplace.entities import Job, Payment` — note: `Payment.job_id`/`session.get(Job, ...)` take `uuid.UUID`, never the raw JSON string):

```python
def _pending_accept(
    client: TestClient, auth: AuthFactory, sid: str, fake: FakeProvider
) -> tuple[str, str]:
    """Set up a job accepted with a pending charge; return (job_id, provider_payment_id)."""
    fake.next_charge_status = PaymentStatus.PENDING
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )
    assert pid is not None
    return str(job["id"]), pid


def test_webhook_success_activates_the_job(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    r = client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_1", "kind": "payment_succeeded", "object_id": pid},
    )
    assert r.json() == {"status": "ok"}
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "accepted"
    assert view["payment_status"] == "succeeded"


def test_webhook_dedup_is_a_noop(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    event = {"event_id": "evt_dup", "kind": "payment_succeeded", "object_id": pid}
    assert client.post("/v1/payments/webhook", json=event).json() == {"status": "ok"}
    assert client.post("/v1/payments/webhook", json=event).json() == {"status": "duplicate"}


def test_webhook_failure_records_but_keeps_waiting(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    client.post(
        "/v1/payments/webhook",
        json={"event_id": "evt_f", "kind": "payment_failed", "object_id": pid},
    )
    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()
    assert view["status"] == "awaiting_payment"  # buyer can still retry confirmation
    assert view["payment_status"] == "failed"


def test_webhook_account_updated_flips_readiness(
    client: TestClient, auth: AuthFactory
) -> None:
    client.post("/v1/seller/payments/onboard", headers=auth("seller", "s1"))
    client.post(
        "/v1/payments/webhook",
        json={
            "event_id": "evt_a",
            "kind": "account_updated",
            "object_id": "acct_fake_s1",
            "payments_ready": False,
        },
    )
    from marketplace.entities import SellerProfile

    with SessionLocal() as s:
        prof = s.get(SellerProfile, "s1")
        assert prof is not None and prof.payments_ready is False


def test_malformed_webhook_is_400(client: TestClient) -> None:
    r = client.post(
        "/v1/payments/webhook", content=b"not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400


def test_payment_timeout_expires_job_and_frees_slot(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    # Age the job past the payment TTL (white-box, same precedent as seed_rating).
    with SessionLocal() as s:
        job = s.get(Job, UUID(job_id))
        assert job is not None
        job.accepted_at = datetime.now(UTC) - timedelta(minutes=999)
        s.commit()

    view = client.get(f"/v1/jobs/{job_id}", headers=auth("buyer", "alice")).json()  # sweep on read
    assert view["status"] == "expired"
    assert fake_payments.cancelled == [pid]
    # The seller's slot is free again.
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "pending"
```

Run: `uv run pytest tests/test_payments.py -q`
Expected: new tests FAIL (404 on `/v1/payments/webhook`; timeout test fails on status).

- [ ] **Step 2: Add the webhook router and dispatch to `src/marketplace/api.py`**

Imports to extend (with their usage, same edit): `Request` from fastapi; `Payout`, `WebhookEvent` in the entities block; `PayoutStatus` in the models block; `PaymentEvent`, `WebhookSignatureError` in the port import.

After the admin router section:

```python
# ---------- Payments router (webhooks) ----------

payments_router = APIRouter(prefix="/v1/payments", tags=["payments"])


def _apply_payment_event(session: Session, event: PaymentEvent) -> None:
    """Route a normalized provider event to the row it affects.

    Unknown kinds and unknown ids are recorded (dedup) and ignored — providers
    emit dozens of event types this app doesn't act on."""
    if event.kind in ("payment_succeeded", "payment_failed"):
        payment = session.scalar(
            select(Payment).where(Payment.provider_payment_id == event.object_id).with_for_update()
        )
        if payment is None:
            return
        if event.kind == "payment_succeeded":
            payment.status = PaymentStatus.SUCCEEDED
            job = session.get(Job, payment.job_id, with_for_update=True)
            if job is not None and job.status == JobStatus.AWAITING_PAYMENT:
                job.status = JobStatus.ACCEPTED
        elif payment.status is not PaymentStatus.SUCCEEDED:
            payment.status = PaymentStatus.FAILED  # late failures never undo a success
    elif event.kind == "account_updated":
        seller = session.scalar(
            select(SellerProfile).where(SellerProfile.provider_account_id == event.object_id)
        )
        if seller is not None and event.payments_ready is not None:
            seller.payments_ready = event.payments_ready
    elif event.kind in ("transfer_paid", "transfer_failed"):
        payout = session.scalar(
            select(Payout).where(Payout.provider_transfer_id == event.object_id).with_for_update()
        )
        if payout is not None:
            payout.status = (
                PayoutStatus.PAID if event.kind == "transfer_paid" else PayoutStatus.FAILED
            )


@payments_router.post("/webhook")
async def payments_webhook(
    request: Request, session: SessionDep, provider: ProviderDep
) -> dict[str, str]:
    """Provider event sink. Unauthenticated by design — authenticity comes from
    the provider's signature, verified in parse_webhook. Duplicates no-op."""
    payload = await request.body()
    try:
        event = provider.parse_webhook(payload, request.headers.get("stripe-signature"))
    except WebhookSignatureError:
        raise HTTPException(status_code=400, detail="invalid webhook signature") from None
    except (PaymentError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed webhook payload") from None
    duplicate = session.scalar(
        select(WebhookEvent).where(WebhookEvent.provider_event_id == event.event_id)
    )
    if duplicate is not None:
        return {"status": "duplicate"}
    session.add(WebhookEvent(provider_event_id=event.event_id, kind=event.kind))
    _apply_payment_event(session, event)
    return {"status": "ok"}
```

Register it with the other routers at the bottom:

```python
app.include_router(payments_router)
```

- [ ] **Step 3: Add the payment-timeout sweep**

Below `_sweep_expired_offers`:

```python
def _sweep_stale_payments(session: Session, provider: PaymentProvider) -> None:
    """Jobs stuck AWAITING_PAYMENT past the TTL expire and free the seller's slot."""
    deadline = _now() - timedelta(minutes=settings.payment_ttl_minutes)
    stale = session.scalars(
        select(Job).where(Job.status == JobStatus.AWAITING_PAYMENT, Job.accepted_at < deadline)
    ).all()
    for job in stale:
        payment = session.scalar(
            select(Payment).where(Payment.job_id == job.id).with_for_update()
        )
        if payment is not None and payment.status is not PaymentStatus.SUCCEEDED:
            try:
                provider.cancel_charge(payment.provider_payment_id)
            except PaymentError:
                continue  # provider hiccup: leave it; the next sweep retries
            payment.status = PaymentStatus.FAILED
        job.status = JobStatus.EXPIRED


def _sweep(session: Session, provider: PaymentProvider) -> None:
    """Everything lazy maintenance does on reads: offer expiry + stale payments."""
    _sweep_expired_offers(session)
    _sweep_stale_payments(session, provider)
```

Switch the three sweep call sites to `_sweep(session, provider)` and add `provider: ProviderDep` to each signature: `get_job_buyer`, `list_offers` (seller), and the admin `sweep` endpoint.

- [ ] **Step 4: Run new tests, full suite, gate**

Run: `uv run pytest tests/test_payments.py -q` → PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → all green.

- [ ] **Step 5: Commit**

```bash
git add src/marketplace/api.py tests/test_payments.py
git commit -m "Add provider webhook endpoint with dedup and payment-timeout sweep

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Complete pays the seller; admin payout list + retry

**Files:**
- Modify: `src/marketplace/api.py:432-456` (`complete_job`), admin router additions
- Modify: `src/marketplace/models.py` (`PayoutOut`)
- Test: `tests/test_payments.py`

**Interfaces:**
- Consumes: `Payout` entity, `TransferResult`, `provider.transfer_to_seller`, `PayoutStatus`.
- Produces: `GET /v1/admin/payouts?status=` → `list[PayoutOut]`; `POST /v1/admin/payouts/{payout_id}/retry` → `PayoutOut`; `models.PayoutOut{id, job_id, seller_id, amount, currency, status, provider_transfer_id, created_at}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payments.py`:

```python
def _completed_job(client: TestClient, auth: AuthFactory, sid: str) -> str:
    onboard_and_avail(client, auth, sid, "s1")
    job = new_job(client, auth, sid, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200, r.text
    return str(job["id"])


def test_complete_transfers_the_payout(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    job_id = _completed_job(client, auth, basic_service)
    payouts = client.get("/v1/admin/payouts", headers=admin).json()
    assert len(payouts) == 1
    assert payouts[0]["job_id"] == job_id
    assert payouts[0]["status"] == "paid"
    assert str(payouts[0]["provider_transfer_id"]).startswith("tr_fake_")


def test_transfer_outage_marks_payout_failed_but_completes(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    fake_payments.fail_next_call = True
    r = client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    assert r.status_code == 200  # the work happened; money owed is recorded, not dropped

    failed = client.get("/v1/admin/payouts", params={"status": "failed"}, headers=admin).json()
    assert len(failed) == 1 and failed[0]["provider_transfer_id"] is None


def test_admin_retries_failed_payout(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    fake_payments.fail_next_call = True
    client.post(f"/v1/seller/jobs/{job['id']}/complete", headers=auth("seller", "s1"))
    payout_id = client.get("/v1/admin/payouts", headers=admin).json()[0]["id"]

    r = client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    # Retrying a paid payout is a 409, not a double transfer.
    assert client.post(f"/v1/admin/payouts/{payout_id}/retry", headers=admin).status_code == 409
```

Run: `uv run pytest tests/test_payments.py -q`
Expected: new tests FAIL (404 on `/v1/admin/payouts`).

- [ ] **Step 2: Add `PayoutOut` to `src/marketplace/models.py`**

After `TransactionOut`:

```python
class PayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    seller_id: str
    amount: Decimal
    currency: str
    status: PayoutStatus
    provider_transfer_id: str | None
    created_at: datetime
```

- [ ] **Step 3: Transfer at completion in `src/marketplace/api.py`**

In `complete_job`: add `provider: ProviderDep` to the signature; the two `repo.get_or_create_*` calls at the end already exist — reuse the seller one. Replace the body after the `seller_payout is None` guard:

```python
    job.status = JobStatus.COMPLETED
    job.completed_at = _now()
    tx = Transaction(
        job_id=job.id,
        buyer_price=job.buyer_price,
        seller_payout=job.seller_payout,
        margin=to_money(job.buyer_price - job.seller_payout),
    )
    session.add(tx)

    # Escrow exit: move the payout to the seller. A transfer failure does NOT
    # fail completion — the work happened; the debt is recorded and retried via
    # POST /v1/admin/payouts/{id}/retry.
    seller = repo.get_or_create_seller(session, seller_id)
    payout = Payout(
        job_id=job.id, seller_id=seller_id, amount=job.seller_payout, currency=settings.currency
    )
    if seller.provider_account_id is None:
        payout.status = PayoutStatus.FAILED  # unonboarded (shouldn't match, but never lose money)
    else:
        try:
            transfer = provider.transfer_to_seller(
                provider_account_id=seller.provider_account_id,
                amount=job.seller_payout,
                currency=settings.currency,
                job_id=str(job.id),
                idempotency_key=f"transfer:{job.id}",
            )
            payout.provider_transfer_id = transfer.provider_transfer_id
            payout.status = transfer.status
        except PaymentError:
            payout.status = PayoutStatus.FAILED
    session.add(payout)

    repo.get_or_create_buyer(session, job.buyer_id).completed_jobs += 1
    seller.completed_jobs += 1
    session.flush()
    return tx
```

- [ ] **Step 4: Admin payout endpoints**

Add `PayoutOut` to the models import block. After `list_transactions` in the admin router:

```python
@admin_router.get("/payouts", response_model=list[PayoutOut])
def list_payouts(
    session: SessionDep,
    status: PayoutStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Payout]:
    stmt = select(Payout)
    if status is not None:
        stmt = stmt.where(Payout.status == status)
    rows = session.scalars(stmt.order_by(Payout.created_at.desc())).all()
    return _paginate(rows, limit, offset)


@admin_router.post("/payouts/{payout_id}/retry", response_model=PayoutOut)
def retry_payout(
    payout_id: UUID, session: SessionDep, admin_id: AdminId, provider: ProviderDep
) -> Payout:
    payout = session.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise HTTPException(status_code=404, detail="payout not found")
    if payout.status is not PayoutStatus.FAILED:
        raise HTTPException(status_code=409, detail=f"payout is {payout.status}, not failed")
    seller = session.get(SellerProfile, payout.seller_id)
    if seller is None or seller.provider_account_id is None:
        raise HTTPException(status_code=409, detail="seller has no payment account yet")
    try:
        transfer = provider.transfer_to_seller(
            provider_account_id=seller.provider_account_id,
            amount=payout.amount,
            currency=payout.currency,
            job_id=str(payout.job_id),
            idempotency_key=f"transfer:{payout.job_id}",  # same key: replays are safe
        )
    except PaymentError:
        raise HTTPException(
            status_code=502, detail="payment provider unavailable, retry"
        ) from None
    payout.provider_transfer_id = transfer.provider_transfer_id
    payout.status = transfer.status
    audit(session, admin_id, "retry_payout", str(payout_id), {})
    return payout
```

- [ ] **Step 5: Run tests, full suite, gate**

Run: `uv run pytest tests/test_payments.py -q` → PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → green.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/api.py src/marketplace/models.py tests/test_payments.py
git commit -m "Transfer seller payout at completion; admin payout list and retry

Transfer failure records a FAILED payout instead of failing the
completion - money owed is never dropped.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Cancel voids or refunds

**Files:**
- Modify: `src/marketplace/api.py:252-261` (`cancel_job`), `:628-638` (`admin_cancel_job`)
- Test: `tests/test_payments.py`

**Interfaces:**
- Consumes: `provider.refund`, `provider.cancel_charge`, `Payment`.
- Produces: `api._release_payment(session: Session, provider: PaymentProvider, job: Job) -> None` — voids a pending charge or refunds a succeeded one; raises `PaymentError` upward.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payments.py`:

```python
def test_buyer_cancels_awaiting_payment_voids_charge(
    client: TestClient, basic_service: str, auth: AuthFactory, fake_payments: FakeProvider
) -> None:
    job_id, pid = _pending_accept(client, auth, basic_service, fake_payments)
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert fake_payments.cancelled == [pid]
    assert fake_payments.refunded == []


def test_admin_cancel_of_paid_job_refunds(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header,
    fake_payments: FakeProvider,
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))  # instant success → paid

    with SessionLocal() as s:
        pid = s.scalar(
            select(Payment.provider_payment_id).where(Payment.job_id == UUID(str(job["id"])))
        )

    r = client.post(f"/v1/admin/jobs/{job['id']}/cancel", headers=admin)
    assert r.status_code == 200
    assert fake_payments.refunded == [pid]
    view = client.get(f"/v1/jobs/{job['id']}", headers=auth("buyer", "alice")).json()
    assert view["payment_status"] == "refunded"
    # The seller's slot is freed.
    job2 = new_job(client, auth, basic_service, "bob")
    assert job2["status"] == "pending"


def test_buyer_still_cannot_cancel_accepted(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job = new_job(client, auth, basic_service, "alice")
    _accept_first_offer(client, auth("seller", "s1"))
    r = client.post(f"/v1/jobs/{job['id']}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 409  # paid + committed: only an admin unwinds this
```

Run: `uv run pytest tests/test_payments.py -q`
Expected: the 3 new tests FAIL (cancel of awaiting_payment 409s today; no refund happens).

- [ ] **Step 2: Add `_release_payment` and wire both cancel endpoints in `src/marketplace/api.py`**

Helper next to `_expire_open_offers`:

```python
def _release_payment(session: Session, provider: PaymentProvider, job: Job) -> None:
    """Undo whatever the job's charge collected: void a pending PI, refund a
    succeeded one. No-op when nothing was charged. Raises PaymentError upward."""
    payment = session.scalar(select(Payment).where(Payment.job_id == job.id).with_for_update())
    if payment is None:
        return
    if payment.status is PaymentStatus.SUCCEEDED:
        provider.refund(payment.provider_payment_id, idempotency_key=f"refund:{job.id}")
        payment.status = PaymentStatus.REFUNDED
    elif payment.status is PaymentStatus.PENDING:
        provider.cancel_charge(payment.provider_payment_id)
        payment.status = PaymentStatus.FAILED  # ponytail: voided lands in FAILED, split if ops needs it
```

`cancel_job` — add `provider: ProviderDep`, widen the allowed states, release money:

```python
@buyer_router.post("/jobs/{job_id}/cancel", response_model=BuyerJobView)
def cancel_job(
    job_id: UUID, session: SessionDep, buyer_id: BuyerId, provider: ProviderDep
) -> Job:
    job = session.get(Job, job_id, with_for_update=True)
    if job is None or job.buyer_id != buyer_id:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in (JobStatus.PENDING, JobStatus.AWAITING_PAYMENT):
        raise HTTPException(status_code=409, detail=f"cannot cancel a {job.status} job")
    _expire_open_offers(session, job.id)
    try:
        _release_payment(session, provider, job)
    except PaymentError:
        raise HTTPException(
            status_code=502, detail="payment provider unavailable, retry"
        ) from None
    job.status = JobStatus.CANCELLED
    return job
```

`admin_cancel_job` — same additions (`provider: ProviderDep` in the signature; the try/except `_release_payment` block inserted after `_expire_open_offers`, before `job.status = JobStatus.CANCELLED`). The existing status guard already permits PENDING/AWAITING_PAYMENT/ACCEPTED — unchanged.

- [ ] **Step 3: Run tests, full suite, gate**

Run: `uv run pytest tests/test_payments.py -q` → PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → green (existing buyer-cancel-PENDING tests unaffected: no payment row exists then).

- [ ] **Step 4: Commit**

```bash
git add src/marketplace/api.py tests/test_payments.py
git commit -m "Void or refund the charge on job cancellation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Client Idempotency-Key middleware

**Files:**
- Create: `src/marketplace/idempotency.py`
- Modify: `src/marketplace/auth.py` (add `peek_principal`)
- Modify: `src/marketplace/api.py` (register middleware)
- Test: `tests/test_idempotency.py`

**Interfaces:**
- Consumes: `IdempotencyRecord` (Task 2), `db.SessionLocal`, `auth._verify`.
- Produces: `auth.peek_principal(authorization: str | None) -> str | None` (returns `"role:sub"`); `idempotency.IdempotencyMiddleware` (pure ASGI); behavior: POST + `Idempotency-Key` header → first response `< 500` stored per (principal, key); replay returns stored body/status; same key on another path → 409; key > 200 chars → 422.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_idempotency.py`:

```python
"""Client Idempotency-Key semantics on money-mutating POSTs."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marketplace.db import SessionLocal
from marketplace.entities import Payment
from tests.conftest import AuthFactory, Header
from tests.test_payments import _accept_first_offer, new_job, onboard_and_avail


def _idem(headers: Header, key: str) -> Header:
    return {**headers, "Idempotency-Key": key}


def test_replayed_accept_returns_stored_response_and_charges_once(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    new_job(client, auth, basic_service, "alice")
    seller = _idem(auth("seller", "s1"), "accept-once")
    offer = client.get("/v1/seller/offers", headers=seller).json()[0]

    r1 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    r2 = client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=seller)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()  # byte-for-byte replay, not a re-execution (which would 409)
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 1


def test_same_key_different_path_conflicts(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    buyer = _idem(auth("buyer", "alice"), "one-key")
    r1 = client.post("/v1/quotes", json={"service_type_id": basic_service}, headers=buyer)
    assert r1.status_code == 200
    r2 = client.post("/v1/jobs", json={"quote_id": r1.json()["id"]}, headers=buyer)
    assert r2.status_code == 409


def test_keys_are_scoped_per_principal(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    a = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        headers=_idem(auth("buyer", "alice"), "k"),
    )
    b = client.post(
        "/v1/quotes",
        json={"service_type_id": basic_service},
        headers=_idem(auth("buyer", "bob"), "k"),
    )
    assert a.status_code == b.status_code == 200
    assert a.json()["id"] != b.json()["id"]  # not a replay across principals


def test_error_responses_replay_too(
    client: TestClient, basic_service: str, auth: AuthFactory
) -> None:
    buyer = _idem(auth("buyer", "alice"), "bad-quote")
    r1 = client.post("/v1/quotes", json={"service_type_id": "nope"}, headers=buyer)
    r2 = client.post("/v1/quotes", json={"service_type_id": "nope"}, headers=buyer)
    assert r1.status_code == r2.status_code == 404


def test_oversized_key_rejected(client: TestClient, auth: AuthFactory) -> None:
    r = client.post(
        "/v1/quotes",
        json={"service_type_id": "x"},
        headers=_idem(auth("buyer", "alice"), "k" * 201),
    )
    assert r.status_code == 422


def test_no_auth_passes_through_to_401(client: TestClient) -> None:
    r = client.post(
        "/v1/quotes", json={"service_type_id": "x"}, headers={"Idempotency-Key": "anon"}
    )
    assert r.status_code == 401
```

Run: `uv run pytest tests/test_idempotency.py -q`
Expected: FAIL — replay/second calls hit real endpoints (409 on second accept, etc.).

- [ ] **Step 2: Add `peek_principal` to `src/marketplace/auth.py`**

After `current_seller`:

```python
def peek_principal(authorization: str | None) -> str | None:
    """Best-effort principal ("role:sub") for middleware. None when absent or
    invalid — the strict endpoint dependencies still produce the real 401."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        claims = _verify(token)
    except HTTPException:
        return None
    return f"{claims.role}:{claims.sub}"
```

- [ ] **Step 3: Create `src/marketplace/idempotency.py`**

Pure ASGI middleware (typed; `BaseHTTPMiddleware`'s response type fights pyright strict):

```python
"""Client-facing idempotency: optional Idempotency-Key header on POSTs.

The first response (any status < 500) is stored per (principal, key) and
replayed byte-for-byte on repeats. The same key on a different path is a 409.
Uses its own short-lived DB sessions, separate from the request's.

ponytail: the store races on truly concurrent duplicates — both execute, the
unique constraint drops one record, and the DB row locks downstream already
make the duplicate call safe. A reserve-then-execute two-phase insert is the
upgrade if exactly-once matters more than simplicity.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import peek_principal
from .db import SessionLocal
from .entities import IdempotencyRecord

MAX_KEY_LENGTH = 200


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        key = headers.get("idempotency-key")
        if key is None:
            await self.app(scope, receive, send)
            return
        if len(key) > MAX_KEY_LENGTH:
            response: Response = JSONResponse(
                {"detail": "Idempotency-Key too long"}, status_code=422
            )
            await response(scope, receive, send)
            return
        principal = peek_principal(headers.get("authorization"))
        if principal is None:
            await self.app(scope, receive, send)  # auth 401s downstream with the real error
            return

        path = str(scope["path"])
        with SessionLocal() as session:
            row = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.principal == principal, IdempotencyRecord.key == key
                )
            )
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

        captured_status = 500
        captured_body = b""

        async def record_send(message: Message) -> None:
            nonlocal captured_status, captured_body
            if message["type"] == "http.response.start":
                captured_status = int(message["status"])
            elif message["type"] == "http.response.body":
                captured_body += bytes(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, record_send)

        if captured_status < 500:
            with SessionLocal() as session:
                session.add(
                    IdempotencyRecord(
                        principal=principal,
                        key=key,
                        path=path,
                        response_status=captured_status,
                        response_body=captured_body.decode("utf-8", errors="replace"),
                    )
                )
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # concurrent duplicate won the insert; fine
```

- [ ] **Step 4: Register in `src/marketplace/api.py`**

With the app assembly (imports land in the same edit):

```python
from .idempotency import IdempotencyMiddleware
```

```python
app.add_middleware(IdempotencyMiddleware)
```

- [ ] **Step 5: Run tests, full suite, gate**

Run: `uv run pytest tests/test_idempotency.py -q` → PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → green. (Webhook POSTs carry no Idempotency-Key header, so the middleware ignores them.)

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/idempotency.py src/marketplace/auth.py src/marketplace/api.py tests/test_idempotency.py
git commit -m "Add client Idempotency-Key replay middleware

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: StripeProvider

**Files:**
- Create: `src/marketplace/payments/stripe_provider.py`
- Modify: `src/marketplace/payments/__init__.py` (selection branch)
- Modify: `pyproject.toml` (via `uv add stripe`)
- Test: `tests/test_stripe_provider.py`

**Interfaces:**
- Consumes: the port types (Task 1), `settings.stripe_secret_key` / `stripe_webhook_secret`.
- Produces: `StripeProvider(secret_key: str, webhook_secret: str)` implementing `PaymentProvider`; `get_provider()` now returns it when `settings.stripe_secret_key` is set.

- [ ] **Step 1: Add the dependency**

Run: `uv add stripe`
Expected: `stripe` added to `[project.dependencies]`, lockfile updated.

- [ ] **Step 2: Write the failing tests (signature math only — no network)**

Create `tests/test_stripe_provider.py`:

```python
"""StripeProvider unit tests: webhook signature verification and event mapping.

No network — we construct a valid Stripe-Signature header with the same HMAC
scheme Stripe uses (t=<ts>,v1=hexdigest of "<ts>.<payload>")."""

import hashlib
import hmac
import json
import time

import pytest

from marketplace.payments.port import WebhookSignatureError
from marketplace.payments.stripe_provider import StripeProvider

SECRET = "whsec_test_secret"


def _signed(payload: bytes) -> str:
    ts = int(time.time())
    mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(kind: str, obj: dict[str, object]) -> bytes:
    return json.dumps(
        {"id": "evt_test_1", "object": "event", "type": kind, "data": {"object": obj}}
    ).encode()


@pytest.fixture
def provider() -> StripeProvider:
    return StripeProvider("sk_test_dummy", SECRET)


def test_valid_signature_maps_payment_succeeded(provider: StripeProvider) -> None:
    payload = _event("payment_intent.succeeded", {"id": "pi_123"})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.event_id == "evt_test_1"
    assert event.kind == "payment_succeeded"
    assert event.object_id == "pi_123"


def test_account_updated_carries_readiness(provider: StripeProvider) -> None:
    payload = _event("account.updated", {"id": "acct_1", "payouts_enabled": True})
    event = provider.parse_webhook(payload, _signed(payload))
    assert event.kind == "account_updated"
    assert event.payments_ready is True


def test_unhandled_kinds_map_to_ignored(provider: StripeProvider) -> None:
    payload = _event("customer.created", {"id": "cus_1"})
    assert provider.parse_webhook(payload, _signed(payload)).kind == "ignored"


def test_bad_signature_rejected(provider: StripeProvider) -> None:
    payload = _event("payment_intent.succeeded", {"id": "pi_123"})
    with pytest.raises(WebhookSignatureError):
        provider.parse_webhook(payload, "t=1,v1=deadbeef")


def test_missing_signature_rejected(provider: StripeProvider) -> None:
    with pytest.raises(WebhookSignatureError):
        provider.parse_webhook(b"{}", None)
```

Run: `uv run pytest tests/test_stripe_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: marketplace.payments.stripe_provider`.

- [ ] **Step 3: Create `src/marketplace/payments/stripe_provider.py`**

```python
"""Stripe adapter: controller-properties connected accounts (the legacy
Standard/Express/Custom types are deprecated), PaymentIntents for buyer
charges, Transfers for payouts, signed webhooks.

Not exercised against live Stripe in this repo (no account on the dev box):
the signature/parse path is unit-tested; the API calls follow current docs.
Run against a Stripe test account before real money."""

from decimal import Decimal
from typing import Any

import stripe

from ..models import PaymentStatus, PayoutStatus
from .port import (
    AccountResult,
    ChargeResult,
    PaymentError,
    PaymentEvent,
    RefundResult,
    TransferResult,
    WebhookSignatureError,
    to_minor_units,
)

# PaymentIntent statuses that are terminal for us; everything else
# (requires_payment_method, requires_confirmation, processing, …) is PENDING.
_PI_STATUS = {"succeeded": PaymentStatus.SUCCEEDED, "canceled": PaymentStatus.FAILED}

_EVENT_KINDS = {
    "payment_intent.succeeded": "payment_succeeded",
    "payment_intent.payment_failed": "payment_failed",
    "account.updated": "account_updated",
    "transfer.reversed": "transfer_failed",
}


class StripeProvider:
    name = "stripe"

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self._client = stripe.StripeClient(secret_key)
        self._webhook_secret = webhook_secret

    def create_seller_account(self, seller_id: str, *, idempotency_key: str) -> AccountResult:
        try:
            acct = self._client.accounts.create(
                params={
                    "controller": {
                        "fees": {"payer": "application"},
                        "losses": {"payments": "application"},
                        "stripe_dashboard": {"type": "express"},
                    },
                    "capabilities": {"transfers": {"requested": True}},
                    "metadata": {"seller_id": seller_id},
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return AccountResult(
            provider_account_id=acct.id, payments_ready=bool(acct.payouts_enabled)
        )

    def onboarding_link(self, provider_account_id: str, return_url: str) -> str:
        try:
            link = self._client.account_links.create(
                params={
                    "account": provider_account_id,
                    "refresh_url": return_url,
                    "return_url": return_url,
                    "type": "account_onboarding",
                }
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return link.url

    def charge_buyer(
        self,
        *,
        buyer_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> ChargeResult:
        try:
            pi = self._client.payment_intents.create(
                params={
                    "amount": to_minor_units(amount),
                    "currency": currency,
                    "automatic_payment_methods": {"enabled": True},
                    "metadata": {"job_id": job_id, "buyer_id": buyer_id},
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return ChargeResult(
            provider_payment_id=pi.id,
            status=_PI_STATUS.get(pi.status, PaymentStatus.PENDING),
            client_secret=pi.client_secret,
        )

    def cancel_charge(self, provider_payment_id: str) -> None:
        try:
            self._client.payment_intents.cancel(provider_payment_id)
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc

    def refund(self, provider_payment_id: str, *, idempotency_key: str) -> RefundResult:
        try:
            re = self._client.refunds.create(
                params={"payment_intent": provider_payment_id},
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        return RefundResult(provider_refund_id=re.id)

    def transfer_to_seller(
        self,
        *,
        provider_account_id: str,
        amount: Decimal,
        currency: str,
        job_id: str,
        idempotency_key: str,
    ) -> TransferResult:
        try:
            tr = self._client.transfers.create(
                params={
                    "amount": to_minor_units(amount),
                    "currency": currency,
                    "destination": provider_account_id,
                    "transfer_group": job_id,
                },
                options={"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PaymentError(str(exc)) from exc
        # Transfers settle synchronously (platform balance → connected balance).
        return TransferResult(provider_transfer_id=tr.id, status=PayoutStatus.PAID)

    def parse_webhook(self, payload: bytes, signature: str | None) -> PaymentEvent:
        if signature is None:
            raise WebhookSignatureError("missing Stripe-Signature header")
        try:
            event = stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except stripe.SignatureVerificationError as exc:
            raise WebhookSignatureError(str(exc)) from exc
        except ValueError as exc:
            raise PaymentError(f"malformed payload: {exc}") from exc
        obj: dict[str, Any] = dict(event.data.object)
        ready: bool | None = None
        if event.type == "account.updated":
            ready = bool(obj.get("payouts_enabled", False))
        return PaymentEvent(
            event_id=event.id,
            kind=_EVENT_KINDS.get(event.type, "ignored"),
            object_id=str(obj.get("id", "")),
            payments_ready=ready,
        )
```

If pyright rejects the SDK's TypedDict param shapes (stub churn between stripe versions), wrap the offending `params=` dict in `cast(Any, {...})` with a one-line comment — keep the adapter thin rather than fighting stubs.

- [ ] **Step 4: Wire selection in `src/marketplace/payments/__init__.py`**

Replace the file body:

```python
"""Provider selection. STRIPE_SECRET_KEY set → Stripe; unset → the deterministic
fake. The fake is a module singleton so scripted test state and the app see the
same instance; the Stripe client is built once, lazily."""

from ..settings import settings
from .fake import FakeProvider
from .port import PaymentProvider

fake_provider = FakeProvider()
_stripe_provider: PaymentProvider | None = None


def get_provider() -> PaymentProvider:
    global _stripe_provider
    if settings.stripe_secret_key:
        if _stripe_provider is None:
            from .stripe_provider import StripeProvider  # lazy: SDK only when configured

            _stripe_provider = StripeProvider(
                settings.stripe_secret_key, settings.stripe_webhook_secret
            )
        return _stripe_provider
    return fake_provider
```

- [ ] **Step 5: Run tests, full suite, gate**

Run: `uv run pytest tests/test_stripe_provider.py -q` → PASS (5 tests).
Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pyright` → green.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/payments/stripe_provider.py src/marketplace/payments/__init__.py tests/test_stripe_provider.py pyproject.toml uv.lock
git commit -m "Add Stripe adapter: controller accounts, PaymentIntents, transfers, signed webhooks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Demo, docs, env example, final verification

**Files:**
- Modify: `scripts/demo.py`, `.env.example`, `README.md`, `CLAUDE.md`, `ROADMAP.md`, `SECURITY.md`

**Interfaces:** none produced — documentation and demo of everything above.

- [ ] **Step 1: Extend `scripts/demo.py`**

Read the script first; match its existing style (it prints deliberately). Two changes:

1. After the seller token is minted and before availability is posted, onboard:

```python
onboard = client.post("/v1/seller/payments/onboard", headers=seller_headers).json()
print(f"seller onboarded: payments_ready={onboard['payments_ready']}")
```

2. After the existing happy-path flow, append an async-payment act showing the webhook path (adapting variable names to the script's):

```python
# --- Act 2: async payment (what real Stripe looks like) ---
from marketplace.payments import fake_provider
from marketplace.models import PaymentStatus

fake_provider.next_charge_status = PaymentStatus.PENDING
# quote → job → accept as before ...
# job lands in AWAITING_PAYMENT with a client_secret on the buyer view
# then the provider confirms:
client.post(
    "/v1/payments/webhook",
    json={"event_id": "evt_demo_1", "kind": "payment_succeeded", "object_id": payment_id},
)
# job is now ACCEPTED; complete it → payout PAID
```

End the script with asserts (its existing pattern) covering: `payments_ready is True`, act-2 job reached `accepted` via webhook, and the final payout status is `paid` (via the admin payouts endpoint).

Run: `uv run python scripts/demo.py`
Expected: exits 0, prints both acts.

- [ ] **Step 2: Update `.env.example`**

Append:

```bash
# Payments — leave unset to use the built-in deterministic fake provider.
# STRIPE_SECRET_KEY=sk_live_...
# STRIPE_WEBHOOK_SECRET=whsec_...
# CURRENCY=usd
# PAYMENT_TTL_MINUTES=30
# ONBOARDING_RETURN_URL=https://your-app.example/onboarded
```

- [ ] **Step 3: Update the docs**

- `README.md`: add a "Payments" section — escrow model in two sentences (charge at accept → platform balance → transfer at complete; the spread stays by construction), fake-vs-Stripe selection via `STRIPE_SECRET_KEY`, new endpoints (`POST /v1/seller/payments/onboard`, `POST /v1/payments/webhook`, `GET /v1/admin/payouts`, `POST /v1/admin/payouts/{id}/retry`), the `Idempotency-Key` header, and the Stripe webhook URL to configure (`/v1/payments/webhook`).
- `CLAUDE.md`: add payments invariants to Non-negotiables: providers are only reached through `payments/port.py` (never import `stripe` outside `stripe_provider.py`); `Payment`/`Payout` record cash movement, `Transaction` stays the margin ledger — don't merge them; `AWAITING_PAYMENT` holds a capacity slot; webhook handling must stay dedup-idempotent. Add to Subtle bits: the fake provider is a module singleton reset by an autouse fixture; charge/refund/transfer outbound idempotency keys are derived from the job id (`charge:{job_id}` etc.) so retries are replays.
- `ROADMAP.md`: move "Payments & payouts" and "Idempotency keys" into Done ✓ (with a line: Stripe adapter unit-tested only — verify against a Stripe test account before real money). "What's still ahead" now leads with notifications/trust&safety; add "disputes/chargebacks + partial refunds" explicitly.
- `SECURITY.md`: extend the update note — webhook endpoint is unauthenticated but signature-verified with replay dedup; `client_secret` is exposed only to the owning buyer while awaiting payment; refunds/voids are admin- or owner-initiated only; client idempotency responses are stored per-principal (no cross-principal replay). Note the residual: fake provider accepts unsigned webhooks by design and must never be selected in production (selection is env-driven).

- [ ] **Step 4: Full final verification**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
uv run python scripts/demo.py
rm -f /tmp/claude-1000/mig.db && DATABASE_URL=sqlite+pysqlite:////tmp/claude-1000/mig.db uv run alembic upgrade head
```

Expected: everything green; demo exits 0; migrations apply from scratch.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py .env.example README.md CLAUDE.md ROADMAP.md SECURITY.md
git commit -m "Document payments: demo acts, env example, README/CLAUDE/ROADMAP/SECURITY

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review checklist (run after writing, fixed inline)

- **Spec coverage:** escrow at accept (T4) · AWAITING_PAYMENT (T1/T4) · payment retry reuses same PI (T4 comment + outbound key) · payment-timeout sweep (T5) · webhook + dedup + signature (T5/T9) · onboarding + readiness gate (T3) · transfer at complete + failure→retry (T6) · cancel void/refund (T7) · client idempotency (T8) · Stripe controller accounts (T9) · fake provider dev/tests (T1) · single currency setting (T1) · docs/demo (T10). Deferred items (disputes, partial refunds, multi-currency, payout schedules, frontend) land in ROADMAP (T10).
- **Type consistency:** `ProviderDep` defined T3, used T4–T7; `_sweep(session, provider)` replaces `_sweep_expired_offers` at call sites in T5 (T4's `get_job_buyer` still calls `_sweep_expired_offers` until T5 — correct, it exists throughout); `onboard_and_avail`/`new_job`/`_accept_first_offer`/`_pending_accept` defined in `tests/test_payments.py` and imported by `tests/test_idempotency.py` (public names, no underscore on the two imported helpers — `_accept_first_offer` is imported too; Python allows it, ruff does not flag cross-test imports).
- **Placeholders:** none — every step carries runnable code or an exact command.
