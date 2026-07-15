# Fee-Aware Margin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record an estimated provider fee on every charge and make both margin reporting and the quote-time margin floor net-of-fees.

**Architecture:** Fees attach to charges, not completions — a `fee_estimate` snapshot is stamped on `Payment` at charge creation from an admin-tunable config (`fee_pct`/`fee_fixed` on the `platform_config` singleton). The margin summary subtracts fees over captured charges (`SUCCEEDED`/`REFUNDED`) — a cash view that finally shows refunded jobs' fee loss. The floor invariant becomes `spread ≥ effective_floor(bp) + estimated_fee(bp)` and is enforced in BOTH places it lives: `matching.passes_floor` (candidate filtering) and the quote-path bump in `api.py`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic (migration #9), Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-07-15-fee-aware-margin-design.md` (approved).

## Global Constraints

- `uv` only — never pip/venv. All commands `uv run ...`.
- **Bare exit codes** — never `cmd | tail` or `cmd | grep` when the exit code gates a decision; run the command bare and check `$?` (or rely on `&&`).
- Pyright strict must stay at 0 errors across `src/` and `tests/`.
- **PostToolUse formatter strips not-yet-used imports** — add an import and its first use in the SAME edit, or re-add the import after writing the usage.
- TDD is binding and audited: write the failing test, RUN it, capture the failing output, then implement. Your report must quote the red-run evidence.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No backticks inside double-quoted `git commit -m`.
- Commit ONLY the files each task lists — never `git add -A`.
- Postgres: `docker compose up -d db` (container `marketplace-db-1`; prefix commands with `sg docker -c "..."` if the socket denies). URL: `postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace`.
- Money is `Decimal`, quantized via `models.to_money` (2 dp, half-up). Never float money.
- Migrations render plain column types (`sa.Numeric(precision=12, scale=2)` for `_MONEY`), never import app internals.
- Work on branch `fee-margin` (created in Task 1 Step 0 from `main`).

---

### Task 1: Fee config plumbing — schema, snapshot, loader, pure helpers, admin endpoint, migration #9

**Files:**
- Modify: `src/marketplace/entities.py` (PlatformConfig ~line 108, Payment ~line 289)
- Modify: `src/marketplace/config.py` (FeeConfig after MarginFloor ~line 27; PricingConfig ~line 37)
- Modify: `src/marketplace/matching.py` (after `effective_floor` ~line 58)
- Modify: `src/marketplace/repo.py` (after `get_platform_config` ~line 56; `load_pricing_config` ~line 60)
- Modify: `src/marketplace/models.py` (FeesBody after MarginFloorBody ~line 550)
- Modify: `src/marketplace/api.py` (GET config dict ~line 1169; new PUT after `update_margin_floor` ~line 1228)
- Create: `migrations/versions/<autogen>_fee_aware_margin.py`
- Test: `tests/test_fees.py` (new file)

**Interfaces:**
- Consumes: `to_money` (models.py), `MarginFloor` (config.py), `repo.get_platform_config`, `repo.audit(session, actor, action, target, detail)`.
- Produces (later tasks rely on these exact names):
  - `config.FeeConfig` — frozen-style dataclass, `pct: Decimal = Decimal(0)`, `fixed: Decimal = Decimal(0)` (zero defaults keep pure-core tests fee-free; the OPERATIVE defaults live on the DB row).
  - `config.PricingConfig.fees: FeeConfig` (default_factory=FeeConfig).
  - `matching.estimated_fee(amount: Decimal, fees: FeeConfig) -> Decimal`
  - `matching.required_spread(buyer_price: Decimal, floor: MarginFloor, fees: FeeConfig) -> Decimal`
  - `repo.fee_config(session: Session) -> FeeConfig`
  - `entities.Payment.fee_estimate: Mapped[Decimal]` (default `Decimal(0)`)
  - `entities.PlatformConfig.fee_pct` (Numeric(5,4), default `Decimal("0.029")`), `.fee_fixed` (`_MONEY`, default `Decimal("0.30")`)
  - `PUT /v1/admin/config/fees`, `"fees"` block in `GET /v1/admin/config`.

- [ ] **Step 0: Branch**

```bash
git checkout -b fee-margin main
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fees.py`:

```python
"""Fee-aware margin: config plumbing, pure fee math, admin endpoint."""

from decimal import Decimal

from fastapi.testclient import TestClient

from marketplace.config import FeeConfig, MarginFloor
from marketplace.db import SessionLocal
from marketplace.matching import estimated_fee, required_spread
from marketplace.repo import fee_config, get_platform_config
from tests.conftest import AuthFactory, Header


def test_estimated_fee_math() -> None:
    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert estimated_fee(Decimal("50.00"), stripe) == Decimal("1.75")
    assert estimated_fee(Decimal("20.00"), stripe) == Decimal("0.88")
    # half-up quantization at the money boundary
    assert estimated_fee(Decimal("25.00"), stripe) == Decimal("1.03")  # 1.025 rounds up
    # zero config means zero fee
    assert estimated_fee(Decimal("50.00"), FeeConfig()) == Decimal("0.00")


def test_required_spread_is_floor_plus_fee() -> None:
    floor = MarginFloor(absolute=Decimal("10"))
    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert required_spread(Decimal("20.00"), floor, stripe) == Decimal("10.88")
    # with zero fees it degrades to the old effective floor
    assert required_spread(Decimal("20.00"), floor, FeeConfig()) == Decimal("10.00")


def test_fee_config_row_defaults() -> None:
    """A fresh platform row defaults to Stripe's standard card rate."""
    with SessionLocal() as session:
        get_platform_config(session)
        fees = fee_config(session)
        session.commit()
    assert fees.pct == Decimal("0.029")
    assert fees.fixed == Decimal("0.30")


def test_admin_fees_endpoint_roundtrip_and_audit(client: TestClient, admin: Header) -> None:
    r = client.put(
        "/v1/admin/config/fees", json={"pct": "0.02", "fixed": "0.25"}, headers=admin
    )
    assert r.status_code == 200
    assert r.json() == {"pct": "0.0200", "fixed": "0.25"}
    cfg = client.get("/v1/admin/config", headers=admin).json()
    assert cfg["fees"] == {"pct": "0.0200", "fixed": "0.25"}
    audit_rows = client.get("/v1/admin/audit", headers=admin).json()
    assert any(a["action"] == "update_fees" for a in audit_rows)


def test_admin_fees_endpoint_validation(client: TestClient, admin: Header) -> None:
    for bad in ({"pct": "1"}, {"pct": "-0.01"}, {"fixed": "-1"}):
        r = client.put("/v1/admin/config/fees", json=bad, headers=admin)
        assert r.status_code == 422, bad
```

Note on `r.json() == {"pct": "0.0200", ...}`: the endpoint returns `str(pc.fee_pct)` and Numeric(5,4) round-trips as `0.0200`. If the first green run shows a different but equivalent string (e.g. `0.02`), assert on `Decimal(r.json()["pct"]) == Decimal("0.02")` instead — equivalence is the requirement, not the rendering.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_fees.py -v
```

Expected: ImportError (`FeeConfig`/`estimated_fee`/`fee_config` don't exist). Capture the output for your report.

- [ ] **Step 3: Implement — config.py**

After `MarginFloor` (~line 27):

```python
@dataclass
class FeeConfig:
    """Provider's estimated cut per captured charge: pct * amount + fixed.

    Zero defaults keep the pure core fee-free unless configured; the running
    system's defaults (Stripe's 2.9% + 30c) live on the platform_config row.
    """

    pct: Decimal = Decimal(0)
    fixed: Decimal = Decimal(0)
```

`PricingConfig` gains a field (needs `field` imported: change line 8 to `from dataclasses import dataclass, field` in the same edit):

```python
@dataclass
class PricingConfig:
    service: ServiceSpec
    buyer_pipeline: list[str]
    seller_pipeline: list[str]
    adjuster_params: dict[str, dict[str, Any]]
    margin_floor: MarginFloor
    matching_strategy: str
    fees: FeeConfig = field(default_factory=FeeConfig)
```

- [ ] **Step 4: Implement — matching.py pure helpers**

After `effective_floor` (line 58-59), before `passes_floor`. Add `FeeConfig` to the existing `from .config import ...` line in the same edit:

```python
def estimated_fee(amount: Decimal, fees: FeeConfig) -> Decimal:
    return to_money(amount * fees.pct + fees.fixed)


def required_spread(buyer_price: Decimal, floor: MarginFloor, fees: FeeConfig) -> Decimal:
    """Minimum spread that nets positive: the floor plus the provider's cut."""
    return effective_floor(buyer_price, floor) + estimated_fee(buyer_price, fees)
```

Do NOT touch `passes_floor` in this task — the floor stays gross until Task 2 flips both enforcement sites atomically.

- [ ] **Step 5: Implement — entities.py**

`PlatformConfig` (~line 108), after `ceiling_multiplier`:

```python
    fee_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.029"))
    fee_fixed: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0.30"))
```

`Payment` (~line 289), after `amount`:

```python
    fee_estimate: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal(0))
```

- [ ] **Step 6: Implement — repo.py loader**

After `get_platform_config` (~line 56):

```python
def fee_config(session: Session) -> FeeConfig:
    pc = get_platform_config(session)
    return FeeConfig(pct=pc.fee_pct, fixed=pc.fee_fixed)
```

Add `FeeConfig` to repo's existing `from .config import ...` line in the same edit. In `load_pricing_config`, add to the `PricingConfig(...)` construction:

```python
        matching_strategy=pc.matching_strategy,
        fees=FeeConfig(pct=pc.fee_pct, fixed=pc.fee_fixed),
```

(Constructed inline from the already-loaded `pc`, not via `fee_config(session)` — no second lookup.)

- [ ] **Step 7: Implement — models.py FeesBody**

After `MarginFloorBody` (~line 550):

```python
class FeesBody(BaseModel):
    pct: Decimal = Field(
        default=Decimal("0.029"), ge=0, lt=1, allow_inf_nan=False, max_digits=5, decimal_places=4
    )
    fixed: Decimal = Field(
        default=Decimal("0.30"), ge=0, allow_inf_nan=False, max_digits=12, decimal_places=2
    )
```

- [ ] **Step 8: Implement — api.py endpoint + config block**

Add `FeesBody` to api.py's `from .models import ...` block (same edit as its use below). In the `GET /v1/admin/config` return dict (~line 1169), after the `"margin_floor"` block:

```python
        "fees": {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)},
```

After `update_margin_floor` (~line 1228):

```python
@admin_router.put("/config/fees")
def update_fees(body: FeesBody, session: SessionDep, admin_id: AdminId) -> dict[str, str]:
    pc = repo.get_platform_config(session)
    pc.fee_pct = body.pct
    pc.fee_fixed = body.fixed
    audit(session, admin_id, "update_fees", "platform", body.model_dump(mode="json"))
    return {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)}
```

(`audit` is already imported in api.py — check the import line; it's used three lines up in `update_margin_floor`.)

- [ ] **Step 9: Migration #9**

```bash
uv run alembic revision -m "fee aware margin"
```

Fill the generated file (style-match `a69df93d854b_notification_prefs.py`; `down_revision` must be `"a69df93d854b"`):

```python
def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column(
            "fee_estimate", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "platform_config",
        sa.Column(
            "fee_pct", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.029"
        ),
    )
    op.add_column(
        "platform_config",
        sa.Column(
            "fee_fixed", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.30"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("platform_config", "fee_fixed")
    op.drop_column("platform_config", "fee_pct")
    op.drop_column("payments", "fee_estimate")
```

- [ ] **Step 10: Run the new tests, then everything — bare exit codes**

```bash
uv run pytest tests/test_fees.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0. No existing test changes behavior in this task (the floor is still gross).

- [ ] **Step 11: Commit**

```bash
git add src/marketplace/config.py src/marketplace/matching.py src/marketplace/entities.py src/marketplace/repo.py src/marketplace/models.py src/marketplace/api.py migrations/versions/ tests/test_fees.py
git commit -m "Add fee config plumbing: estimate helpers, platform columns, admin endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Fee-aware floor — both enforcement sites atomically

**Files:**
- Modify: `src/marketplace/matching.py` (`passes_floor` line 62-63, `_priced` line 66-76)
- Modify: `src/marketplace/api.py` (quote path, the `if probe is not None:` block ~line 542-553)
- Modify: `tests/test_margin_floor.py` (two expected values change; new tests appended)

**Interfaces:**
- Consumes: `matching.required_spread(buyer_price, floor, fees)`, `matching.estimated_fee`, `config.FeeConfig`, `PricingConfig.fees` (all from Task 1).
- Produces: `matching.passes_floor(buyer_price: Decimal, payout: Decimal, floor: MarginFloor, fees: FeeConfig) -> bool` (fees now REQUIRED — no default; a silent zero-fee call is the bug class this feature kills). The quote-path invariant: `buyer_price - probe >= required_spread(buyer_price, floor, fees)`.

**Why one task:** the quote bump guarantees "at least one candidate passes the floor"; `passes_floor` inside `_priced` is what actually filters at match time. If one goes net and the other stays gross, quotes get issued that can never match (or matches violate the net floor). Flip both in one commit.

- [ ] **Step 1: Update expected values + write the new failing tests**

In `tests/test_margin_floor.py`, the fee default (2.9% + 30¢ on the platform row) changes two expectations. Update in place:

```python
def test_floor_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    # base spread 20-14=6; floor 10 + fee(bp) → first ceil lands on 25, which
    # STILL undershoots (25-14=11.00 < 10+fee(25)=11.03) — the verify loop
    # bumps once more. 26-14=12.00 >= 10+fee(26)=11.05.
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "26.00"


def test_floor_pct_bumps_quote_up(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    _available(client, auth, basic_service)
    # pct 0.5: required grows ~0.529 per bumped unit — the loop walks from the
    # first ceil (25) to the fixed point: 31-14=17.00 >= 15.50+fee(31)=16.70.
    client.put("/v1/admin/config/margin_floor", json={"pct": 0.5}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "31.00"
```

Append these new tests:

```python
def test_zero_fees_restore_gross_floor(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Pinning fees to zero recovers the pre-fee bump math exactly."""
    _available(client, auth, basic_service)
    client.put("/v1/admin/config/fees", json={"pct": "0", "fixed": "0"}, headers=admin)
    client.put("/v1/admin/config/margin_floor", json={"absolute": 10}, headers=admin)
    assert _quote_price(client, basic_service, auth) == "24.00"


def test_fee_alone_bumps_tight_spread(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """No floor configured at all: the fee IS the floor. A payout within the
    fee of the buyer price forces a bump — the platform never signs a
    money-losing job."""
    _available(client, auth, basic_service)
    # Push the payout to ~19.6 via a tier multiplier so base spread 0.4 < fee 0.88.
    client.put(
        "/v1/admin/config/pipelines/" + basic_service,
        json={"buyer": [], "seller": ["tier_multiplier"]},
        headers=admin,
    )
    client.put(
        "/v1/admin/config/adjusters/tier_multiplier",
        json={"standard": 1.4},
        headers=admin,
    )
    price = _quote_price(client, basic_service, auth)
    assert price != "20.00"  # bumped
    # invariant, not a magic number: spread >= fee at the final price
    from decimal import Decimal

    from marketplace.config import FeeConfig
    from marketplace.matching import estimated_fee

    stripe = FeeConfig(pct=Decimal("0.029"), fixed=Decimal("0.30"))
    assert Decimal(price) - Decimal("19.60") >= estimated_fee(Decimal(price), stripe)


def test_ceiling_still_rejects_on_final_target(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    client.put(
        "/v1/admin/config/margin_floor",
        json={"absolute": 100, "ceiling_multiplier": 1.5},
        headers=admin,
    )
    _available(client, auth, basic_service)
    r = client.post(
        "/v1/quotes", json={"service_type_id": basic_service}, headers=auth("buyer", "a")
    )
    assert r.status_code == 422
    assert "ceiling" in r.json()["detail"]
```

IMPORTANT adaptation notes for the implementer:
- Check how the existing adjuster-params/pipeline admin routes are actually shaped before writing `test_fee_alone_bumps_tight_spread` — mirror `test_matching.py`'s `_tiered_pipeline` helper (line ~20-40 of that file) rather than the guessed routes above; the INTENT (payout 19.6 vs price 20, fee forces a bump, invariant assert) is binding, the plumbing is not. Move the imports to the top of the file per house style.
- `test_no_floor_uses_base_price` and `test_no_supply_no_floor_probe` stay `"20.00"` — base spread 6 clears the 0.88 fee; no edit. If they fail, your implementation is wrong, not the test.
- `test_floor_above_ceiling_rejects` should keep passing unchanged (superseded by the new final-target test but kept).

- [ ] **Step 2: Run to verify the right failures**

```bash
uv run pytest tests/test_margin_floor.py -v
```

Expected: the two updated bump tests FAIL (still getting 24.00 — gross floor), `test_zero_fees_restore_gross_floor` PASSES already-ish or fails on ordering, `test_fee_alone_bumps_tight_spread` FAILS (no bump happens). Capture output.

- [ ] **Step 3: Implement — matching.py**

```python
def passes_floor(
    buyer_price: Decimal, payout: Decimal, floor: MarginFloor, fees: FeeConfig
) -> bool:
    return (buyer_price - payout) >= required_spread(buyer_price, floor, fees)
```

In `_priced` (line ~73):

```python
        if passes_floor(buyer_price, payout, cfg.margin_floor, cfg.fees):
```

- [ ] **Step 4: Implement — api.py quote path**

Replace the `if probe is not None:` block (~line 542-553). Add `required_spread` to the `from .matching import ...` line (line 70) in the same edit; `effective_floor` drops out of the import if now unused (the formatter will strip it — that's correct):

```python
    if probe is not None:
        required = required_spread(buyer_price, cfg.margin_floor, cfg.fees)
        if buyer_price - probe < required:
            # Round the corrected price UP to a whole unit so it isn't pinned to
            # exactly probe + required (which would leak the seller's payout).
            # Both the pct floor and the fee grow with the bumped price, so one
            # ceil can undershoot — walk whole units until the invariant holds,
            # bounded by the ceiling.
            ceiling = to_money(cfg.service.base_buyer_price * cfg.margin_floor.ceiling_multiplier)
            target = to_money(math.ceil(probe + required))
            while target <= ceiling and target - probe < required_spread(
                target, cfg.margin_floor, cfg.fees
            ):
                target += 1
            if target > ceiling:
                raise HTTPException(
                    status_code=422,
                    detail="cannot quote: required margin exceeds the configured price ceiling",
                )
            buyer_price = target
```

- [ ] **Step 5: Run the floor tests, then the full suites**

```bash
uv run pytest tests/test_margin_floor.py tests/test_matching.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all green. If OTHER tests fail, the cause will be a deliberately tight spread now caught by the fee floor — fix the test by either pinning fees to zero for that test (`client.put("/v1/admin/config/fees", json={"pct": "0", "fixed": "0"}, headers=admin)`) when fee behavior is irrelevant to its intent, or updating the expected value when the test is about pricing. NEVER weaken the invariant in src to make a test pass. List every such edit in your report.

- [ ] **Step 6: Commit**

```bash
git add src/marketplace/matching.py src/marketplace/api.py tests/test_margin_floor.py
git commit -m "Make the margin floor fee-aware at both enforcement sites

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Include any other test files Step 5 forced you to touch in the `git add` — listed explicitly, not `-A`.)

---

### Task 3: Stamp the charge, report net-of-fees

**Files:**
- Modify: `src/marketplace/api.py` (charge site ~line 1017; `margins_summary` ~line 1692)
- Modify: `src/marketplace/models.py` (`MarginSummaryOut` ~line 430)
- Test: `tests/test_fees.py` (append)

**Interfaces:**
- Consumes: `matching.estimated_fee`, `repo.fee_config` (Task 1); `Payment.fee_estimate` column (Task 1); existing fixtures/helpers `onboard_and_avail`, `new_job` from `tests.test_payments`, `fake_provider` singleton.
- Produces: `MarginSummaryOut.fees_estimated: Decimal`, `MarginSummaryOut.platform_margin_net_of_fees: Decimal`; charge rows carry a nonzero `fee_estimate` snapshot.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fees.py` (hoist any new imports to the top block — `select`, `Payment`, `PaymentStatus`, helpers from `tests.test_payments`; mirror how `tests/test_payments.py` drives accept/cancel/webhook flows rather than inventing new plumbing):

```python
def test_charge_stamps_fee_snapshot(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """The estimate is stamped from config at charge time; later config
    changes never rewrite it."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job_id = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))

    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(job_id))).one()
        stamped = payment.fee_estimate
        price = payment.amount
    assert stamped == to_money(price * Decimal("0.029") + Decimal("0.30"))

    client.put("/v1/admin/config/fees", json={"pct": "0.10", "fixed": "5"}, headers=admin)
    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(job_id))).one()
        assert payment.fee_estimate == stamped  # snapshot, not live


def test_summary_counts_refunded_fee_the_roadmap_hole(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """Charge → buyer cancels → full refund. No Transaction ever books, but
    the fee was still paid: the summary must show it."""
    onboard_and_avail(client, auth, basic_service, "s1")
    job_id = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    r = client.post(f"/v1/jobs/{job_id}/cancel", headers=auth("buyer", "alice"))
    assert r.status_code == 200

    with SessionLocal() as s:
        payment = s.scalars(select(Payment).where(Payment.job_id == UUID(job_id))).one()
        assert payment.status == PaymentStatus.REFUNDED
        fee = payment.fee_estimate

    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(summary["fees_estimated"]) == fee
    assert Decimal(summary["platform_margin"]) == Decimal("0.00")
    assert Decimal(summary["platform_margin_net_of_fees"]) == -fee


def test_summary_excludes_uncaptured_charges(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    """A PENDING (unconfirmed) charge has cost nothing yet — excluded."""
    onboard_and_avail(client, auth, basic_service, "s1")
    fake_provider.next_charge_status = PaymentStatus.PENDING
    job_id = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    assert job_id  # charge parked AWAITING_PAYMENT

    summary = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(summary["fees_estimated"]) == Decimal("0.00")


def test_summary_net_math_exact(
    client: TestClient, basic_service: str, auth: AuthFactory, admin: Header
) -> None:
    onboard_and_avail(client, auth, basic_service, "s1")
    job_id = new_job(client, auth, basic_service, "alice")
    offer = client.get("/v1/seller/offers", headers=auth("seller", "s1")).json()[0]
    client.post(f"/v1/seller/offers/{offer['id']}/accept", headers=auth("seller", "s1"))
    client.post(f"/v1/seller/jobs/{job_id}/complete", headers=auth("seller", "s1"))

    s = client.get("/v1/admin/margins/summary", headers=admin).json()
    assert Decimal(s["fees_estimated"]) > 0
    assert Decimal(s["platform_margin_net_of_fees"]) == (
        Decimal(s["platform_margin"]) + Decimal(s["adjustments_net"]) - Decimal(s["fees_estimated"])
    )
```

Adaptation note: the cancel route shape (`POST /v1/jobs/{id}/cancel`) and the accept/complete plumbing must mirror what `tests/test_payments.py` and `tests/test_lifecycle.py` actually do — copy their idiom (including any idempotency headers those POSTs require). The four test INTENTS are binding: snapshot immutability, refunded-fee counted with negative net, uncaptured excluded, exact net identity.

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_fees.py -v
```

Expected: new tests FAIL — `fee_estimate` is 0 (never stamped) and `fees_estimated` KeyError. Capture output.

- [ ] **Step 3: Implement — stamp at the charge site**

api.py ~line 1017, inside `session.add(Payment(...))`, after `amount=job.buyer_price,`. Add `estimated_fee` to the `from .matching import ...` line in the same edit:

```python
            fee_estimate=estimated_fee(job.buyer_price, repo.fee_config(session)),
```

- [ ] **Step 4: Implement — models.py**

`MarginSummaryOut` (~line 430), after `adjustments_net`:

```python
    fees_estimated: Decimal
    platform_margin_net_of_fees: Decimal
```

- [ ] **Step 5: Implement — summary**

In `margins_summary` (api.py ~1692), after the `adjustments_net` computation:

```python
    fees = sum(
        (
            p_fee
            for p_fee in session.scalars(
                select(Payment.fee_estimate).where(
                    Payment.status.in_((PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED))
                )
            ).all()
        ),
        to_money(0),
    )
```

And in the returned `MarginSummaryOut(...)`:

```python
        fees_estimated=to_money(fees),
        platform_margin_net_of_fees=to_money(margin + adjustments_net - fees),
```

Add a docstring line to the endpoint function (this is the documented cash-view semantic from the spec):

```python
    """Margin summary. Fee fields are a CASH view: a fee is sunk the moment a
    charge captures, so refunded jobs' fees show as losses and charged-but-in-
    flight jobs dip net until their margin books at completion."""
```

- [ ] **Step 6: Run tests, then everything**

```bash
uv run pytest tests/test_fees.py -v
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
```

Expected: all exit 0. `test_end_to_end.py:57` and `test_disputes.py` summary asserts read existing keys only — they must NOT need edits; if they fail, investigate before touching them.

- [ ] **Step 7: Commit**

```bash
git add src/marketplace/api.py src/marketplace/models.py tests/test_fees.py
git commit -m "Stamp fee estimate on every charge, report net-of-fees margin

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Demo act 7, docs, full merge gates

**Files:**
- Modify: `scripts/demo.py` (after step 20, before the final print; update the final print line)
- Modify: `ROADMAP.md`, `README.md`, `SECURITY.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: demo proves the net-of-fees summary; docs record fee-aware margin shipped.

- [ ] **Step 1: Add the demo act**

After step 20's block, before the final `print(`. Add `from decimal import Decimal` to the demo's import block in the same edit:

```python
    # --- Act 7: fee-aware margin (the summary matches the bank account) ---
    print("21. Fees: the margin summary is net of the provider's estimated cut")
    s3 = c.get("/v1/admin/margins/summary", headers=admin).json()
    fees = Decimal(s3["fees_estimated"])
    net = Decimal(s3["platform_margin_net_of_fees"])
    assert fees > 0, s3
    assert net == Decimal(s3["platform_margin"]) + Decimal(s3["adjustments_net"]) - fees, s3
    print(f"   fees_estimated={fees}  margin gross={s3['platform_margin']}  net_of_fees={net}")
```

Extend the final summary print's message to end with `"...notification mute/unmute enforced at enqueue, margin reported net of provider fees."`

- [ ] **Step 2: Run the demo**

```bash
uv run python scripts/demo.py
```

Expected: exit 0, all 21 steps print, both new assertions pass.

- [ ] **Step 3: Update the docs**

- `ROADMAP.md`: "What's still ahead" item 2 (processing fees) moves to Done — add a `**Fee-aware margin (done):**` paragraph under "Where we are" (style-match the notification-preferences one: fee_estimate snapshot at charge time, admin-tunable pct+fixed defaulting to Stripe's 2.9%+30¢, cash-view `fees_estimated`/`platform_margin_net_of_fees` in the summary, floor enforced net-of-fees at both sites, migration #9) and a matching entry in the `Done ✓` run. Renumber the remaining "still ahead" list (observability becomes #2, and so on).
- `SECURITY.md`: a short section adjacent to the money invariants: `fee_estimate` is an ESTIMATE (pct+fixed snapshot at charge time), not reconciled provider actuals; pre-migration payment rows carry 0; the margin floor is enforced net of the estimate at quote/match time so floor-priced jobs cannot be signed at a loss.
- `README.md`: `PUT /v1/admin/config/fees` in the admin endpoint list; mention the two new summary fields where the margins summary is described.
- `CLAUDE.md`: migration count 8 → 9 in the notification-preferences sentence (line ~150: "migration #8, 8 total" → keep #8 for prefs, change the total to 9 — reword as needed); add one non-negotiable bullet under the money bullet: the floor check is net-of-fees via `matching.required_spread` — never compare the floor against gross spread, and `Payment.fee_estimate` is a stamp-time snapshot, never recomputed.

- [ ] **Step 4: Full gates — bare exit codes, both backends, fresh-volume migrations**

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run pytest
uv run python scripts/demo.py
docker compose down -v && docker compose up -d db && sleep 3
DATABASE_URL=postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace uv run alembic upgrade head
```

Expected: every command exits 0; the alembic run applies exactly 9 migrations from scratch.

- [ ] **Step 5: Commit**

```bash
git add scripts/demo.py ROADMAP.md README.md SECURITY.md CLAUDE.md
git commit -m "Document fee-aware margin: demo act 7, roadmap item done

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes (already applied)

- Spec coverage: config columns/dataclass/loader/helper/admin endpoint/migration → Task 1; fee-aware floor at BOTH sites (spec §5 plus the `passes_floor` site the spec's quote-path section implies) → Task 2; charge stamp + summary (spec §3-4) → Task 3; demo/docs (spec §7) → Task 4. Non-goals untouched (no Transaction changes, no provider surface).
- The two-site floor atomicity is called out where it bites (Task 2 header) — the spec text focused on the quote path; candidate filtering via `passes_floor` was found in code review of `_priced` and MUST move in the same commit.
- Expected values `26.00`/`31.00` were hand-computed (half-up `to_money` at each step) and the loop-fires-once/loop-walks-six cases are asserted in comments; if the green run disagrees, re-derive by hand before trusting either.
- Type consistency: `FeeConfig` fields `pct`/`fixed` everywhere; `required_spread(buyer_price, floor, fees)` argument order consistent across matching.py/api.py/tests; `fees_estimated`/`platform_margin_net_of_fees` names identical in models/api/tests/demo.
