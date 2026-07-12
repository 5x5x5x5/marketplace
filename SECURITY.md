# Security posture

A full read-only sweep was done on the v1 scaffold; the **safe-to-pilot
hardening** closed the exploitable findings (status table below). The subsequent
**template build** moved state to Postgres and changed a few security-relevant
mechanics — see the update note first.

## Update — template build

- **Concurrency is now the database's job.** The process-wide lock is gone;
  quote consumption, job/offer status transitions, and seller-capacity checks use
  `SELECT … FOR UPDATE` row locks. On Postgres these are real; the SQLite test
  backend serializes writes, so the deterministic guard tests hold on both, and a
  true-parallel test can run against Postgres via `DATABASE_URL`.
- **Tokens now expire** (`exp` claim; `TOKEN_TTL_HOURS`), closing the
  never-expiring-token gap. Still pilot-grade HMAC — not production auth.
- **Seller capacity** is enforced under a row lock on accept, so a seller can't
  exceed their configured concurrent-job limit even under racing accepts.
- **Money is `Decimal`** end-to-end; the margin floor is enforced on quantized
  values, removing the float sub-floor-leakage drift (old M3).
- **Residual (Low, unchanged):** a hand-crafted non-compliant JSON body
  (`NaN`/`Infinity`) is rejected (never stored) but currently surfaces as a 500
  during error serialization rather than a clean 422. Needs a global error
  envelope — a roadmap item.

## Update — payments

- **The webhook endpoint (`POST /v1/payments/webhook`) is unauthenticated but
  signature-verified.** There's no bearer token to check — the provider can't
  present one — so authenticity comes from `parse_webhook` validating the
  provider's signature header before anything is applied; an invalid signature
  is a 400. Every event is recorded in `WebhookEvent` keyed on the provider's
  event id before it's applied, so a replayed delivery (the provider retries on
  a slow 2xx, or an attacker replays a captured payload) is a no-op, not a
  double-apply.
- **`client_secret` is exposed only to the owning buyer, and only while a
  payment is awaited.** It's `None` once the charge succeeds and never appears
  on any seller or admin view — `BuyerJobView` is the one place it's returned.
- **Refunds/voids are admin- or owner-initiated only.** Job cancellation runs
  through the same buyer/admin-owned cancel path as before; there's no
  standalone refund endpoint a caller could hit directly.
- **Client idempotency responses are stored per-principal.** `IdempotencyRecord`
  is keyed on `(principal, key)` (`idempotency.py`), so one caller can never
  replay another's cached response even if the header value collided.
- **Residual:** the fake payment provider accepts unsigned webhook JSON by
  design (`payments/fake.py` — there's no real provider to sign anything in
  dev/tests) and must never be reachable in production. Selection is
  env-driven (`STRIPE_SECRET_KEY`) with no runtime override, so this is a
  deployment-configuration risk, not a code path an attacker can toggle.

## Threat model (pilot)

Identity comes from an authenticated principal, never from a request body or
query param. Three roles: `buyer`, `seller`, `admin`. Tokens are HMAC-signed
(`src/marketplace/auth.py`) — **pilot-grade** (shared secret, no user store, no
rotation). This is enough to give real users distinct, unspoofable identities
without a database; it is **not** production auth. See `ROADMAP.md` for the
upgrade path (real user store + provider).

Set `MARKETPLACE_SECRET` in any non-local environment. The dev fallback secret
is insecure by design and must never be used where untrusted clients can reach
the API.

## Findings and status

All line references were against the v1 scaffold. "Fixed" means closed on the
hardening branch with a regression test in `tests/test_auth_and_hardening.py`.

### Critical — identity was a caller-supplied string
| ID | Finding | Status |
|----|---------|--------|
| C1 | `/admin/*` fully open — anyone could dump the both-sides ledger and re-tune pricing | **Fixed** — router gated behind `require_admin` |
| C2 | Seller could read `buyer_price` via `GET /jobs/{id}?role=buyer` | **Fixed** — buyer-role + ownership enforced; `role` param removed |
| C3 | Buyer could read `seller_payout` via `GET /jobs/offered?seller_id=X` | **Fixed** — seller derived from token, not query |
| C4 | Full seller impersonation on `accept`/`complete` | **Fixed** — seller derived from token |

### High
| ID | Finding | Status |
|----|---------|--------|
| H1 | Quote double-consume (TOCTOU) → two jobs from one quote | **Fixed** — module lock; concurrency test |
| H2 | Double-complete race → duplicate transaction | **Fixed** — locked status CAS; concurrency test |
| H3 | Unknown adjuster name in a pipeline → 500 on every quote | **Fixed** — validated against `REGISTRY` at config time (422) |
| H4 | Unbounded memory + no rate limit | **Partly fixed** — expired quotes swept on write, active-quote cap. Rate limiting deferred to the gateway (see ROADMAP) |
| H5 | `margin_floor` accepted NaN/negative/unbounded | **Fixed** — validated `MarginFloorBody` (finite, bounded) |

### Medium
| ID | Finding | Status |
|----|---------|--------|
| M1 | `inf` base prices passed `Field(gt=0)` | **Fixed** — `allow_inf_nan=False` |
| M2 | `adjuster_params` unbounded → negative/inf prices | **Fixed** — clamped at read time (`pricing._bounded`) |
| M3 | Floor enforced on unrounded values, booked on rounded | **Partly fixed** — round-then-check. Full `Decimal` migration deferred |
| M4 | `available_for` iterated a live dict → 500 | **Fixed** — `list()` snapshot |
| M5 | Rejection message + exact bump leaked min payout | **Fixed** — generic message; corrected price rounded up off the exact grid |
| M6 | New-buyer-discount farming via rotating `buyer_id` | **Fixed** — buyer identity is authenticated |
| M7 | No pagination on list endpoints | **Fixed** — `limit`/`offset` (capped) |

### Low / deferred
- **L1** ID-length caps — **Fixed** (`max_length` on body/id fields).
- **L2** `POST /jobs` bound no buyer — **Fixed** (quote ownership checked).
- **L3** No CORS/TrustedHost/security-header middleware — **Deferred** to deploy time.
- **N1 (new, Low)** A hand-crafted non-compliant JSON body (`NaN`/`Infinity`) is
  correctly rejected (value never stored) but currently surfaces as a 500 during
  error serialization rather than a clean 422. Requires a non-standard client.
  Fold into the "errors never 500" roadmap item.

## Confirmed sound (not re-touched)
uuid4 IDs (unguessable) · no dynamic code execution from config (pure dict
lookups) · view-model field separation (`BuyerJobView`/`SellerJobView`,
`extra="forbid"`) — the old leaks were wrong-endpoint *access*, not field bleed ·
dependencies current, no known CVEs.
