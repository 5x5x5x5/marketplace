# Security posture

This is a scaffold on its way to a real, multi-user marketplace template. A full
read-only sweep was done on the v1 scaffold; the **safe-to-pilot hardening**
branch closed the exploitable findings. Status below is against that branch.

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
