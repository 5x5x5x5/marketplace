# Security posture

A full read-only sweep was done on the v1 scaffold; the **safe-to-pilot
hardening** closed the exploitable findings (status table below). Four
updates followed, in order: the **template build** (moved state to Postgres),
**payments** (added an escrow provider), **real-user auth** (replaced the
pilot HMAC tokens with DB-backed sessions), and **disputes** (added
arbitration over the escrow — partial refunds/clawbacks and chargeback
recording) — see the update notes below.

## Update — template build

- **Concurrency is now the database's job.** The process-wide lock is gone;
  quote consumption, job/offer status transitions, and seller-capacity checks use
  `SELECT … FOR UPDATE` row locks. On Postgres these are real; the SQLite test
  backend serializes writes, so the deterministic guard tests hold on both, and a
  true-parallel test can run against Postgres via `DATABASE_URL`.
- **Tokens now expire** (`exp` claim; `TOKEN_TTL_HOURS`), closing the
  never-expiring-token gap. Still pilot-grade HMAC — not production auth.
  *(Superseded — see "Update — real-user auth" below: HMAC tokens and
  `TOKEN_TTL_HOURS` are deleted, not just expired.)*
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
  payment is awaited.** The secret is populated exclusively by `_buyer_view`
  (`api.py`), for the owning buyer, while the job is `AWAITING_PAYMENT`; it's
  `None` once the charge succeeds. The admin job routes reuse the `BuyerJobView`
  schema but bypass `_buyer_view` enrichment, so `payment_status`/`client_secret`
  are always `None` there, and no seller view carries the field at all.
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
- **Residual:** a DB commit failure after a successful provider mutation leaves
  the provider briefly ahead of the database — charge and refund self-heal on
  retry via idempotency-key replay, void self-heals via the idempotent cancel
  (already-canceled counts as success), and a transactional outbox is the
  eventual upgrade path.

## Update — real-user auth

Pilot-grade HMAC tokens (`mint_token`, `MARKETPLACE_SECRET`) are gone —
deleted, not deprecated. Identity now resolves through DB-backed sessions:

- **Sessions are opaque bearers, hashed at rest.** `POST /v1/auth/signup` and
  `POST /v1/auth/login` issue a random 32-byte token; only its sha256 is
  stored (`auth_sessions.token_hash`). A leaked database dump doesn't hand out
  usable bearer tokens. Every authenticated request does one indexed lookup
  (`token_hash` + `expires_at > now`) to resolve `(role, user_id)`.
- **Sessions are revocable, not just expiring.** Logout deletes the one row
  for that token. A password reset deletes *every* session row for that user
  (`confirm_password_reset`) — a stolen token stops working the moment the
  owner resets their password. The same row-deletion path is where a future
  ban/suspend would hook in. Sessions also carry a TTL
  (`SESSION_TTL_HOURS`, default 72); expired rows are left for the lazy sweep
  rather than actively purged.
- **Passwords are argon2id via `pwdlib`** (`auth.hash_password`/
  `verify_password`), never stored or logged in plaintext.
- **Login gives no account-existence signal.** An unknown email still runs a
  password verify against a fixed dummy hash before returning the same 401 as
  a wrong password, so response timing doesn't distinguish "no such account"
  from "wrong password."
- **Password-reset-request is uniform-200 by design** (`POST
  /v1/auth/password-reset/request` always replies `{"status": "ok"}`,
  independent of whether the email/role exists) — no enumeration via status
  code or body.
- **One account is one email + one role.** The same email may hold a separate
  buyer account and seller account; `(email, role)` is the uniqueness key, not
  `email` alone. There's no self-serve admin signup — the admin account is
  seeded once at startup from `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- **Email verification and password reset ride the same `EmailSender` port**
  (`src/marketplace/mail.py`). The shipped `ConsoleEmailSender` logs instead of
  sending. Mail-send failure is a guarded boundary: `_issue_email_token`
  catches and logs, so a broken/slow mail provider never fails or
  fingerprints the enclosing request (signup still 201s; reset-request still
  200s) — the token row is flushed-pending on the request's session and
  commits at request end; swallowing the send failure is what lets that
  commit proceed, so the user can re-request.

**Residuals, named rather than hidden:**
- **No login rate-limiting.** Brute-forcing a password is not throttled at
  the app layer; this is deliberately deferred to the gateway (see
  `ROADMAP.md`'s API-hardening item), same posture as the rest of the API.
- **A timing delta remains on `password-reset/request`.** The response body
  and status code are identical either way, but the exists-branch does
  strictly more work (an `EmailToken` insert plus a send attempt) than the
  ghost branch, so wall-clock time leaks a faint signal. Closing it needs
  constant-time work on the ghost path, which isn't built. Accepted at pilot
  grade alongside the no-rate-limiting posture above.
- **Email verification gates nothing yet.** `POST /v1/auth/verify` flips
  `email_verified`, but no endpoint checks that flag — signup and login both
  work on an unverified account, because the console adapter can't prove
  mail was actually deliverable. A fork wiring in a real sender is expected
  to add the gate at the same time.

## Update — disputes

- **Opening a dispute is buyer-only, with ownership, window, and one-per-job
  guards.** `POST /v1/jobs/{id}/dispute` requires the caller's session to
  resolve to the job's own buyer (404 otherwise, same not-yours pattern as
  job views); the job must be `COMPLETED` and within `DISPUTE_WINDOW_DAYS` of
  `completed_at` (409 once the window elapses); a job can carry at most one
  dispute row, ever (409 on a second attempt). A Stripe chargeback on the
  same job annotates that existing row instead of creating a duplicate.
- **Resolving a dispute is admin-only, with bounds validated at the trust
  boundary.** `POST /v1/admin/disputes/{id}/resolve` requires an admin
  session; `refund_amount`/`clawback_amount` are quantized and range-checked
  (0..`buyer_price`, 0..`seller_payout`) before either provider call, so a
  hand-crafted body can never authorize refunding or clawing back more than
  the job itself moved. The two provider legs (partial refund, partial
  transfer reversal) are each idempotent by a dispute-scoped key
  (`refund:{job_id}:dispute`, `reversal:{job_id}:dispute` — deliberately
  distinct from the cancel path's keys); a failure on either leg records
  nothing, and a retry replays the succeeded leg instead of double-moving
  money. `Payment.status` is never touched by a partial refund — the charge
  stays `SUCCEEDED`, so a partial refund can never be mistaken for (or
  collide with) the cancel path's full `REFUNDED` state.
- **The chargeback webhook branch never 500s on an unknown or replayed
  event.** `charge.dispute.created`/`closed` locate the `Payment` by
  `related_id`; an id that doesn't map to a known payment is recorded (dedup
  ledger) and ignored, not a crash. The branch rides the existing
  signature-verified, `WebhookEvent`-deduped `/v1/payments/webhook`, so a
  replayed chargeback event no-ops like every other event kind. An admin's
  arbitration outcome (`resolved`) is preserved even if a chargeback closes
  on the same job afterward — the loss/fee still lands in the `adjustments`
  ledger, but the `status` field keeps recording the arbitration outcome
  rather than being overwritten; a still-`open` dispute lets the latest
  provider outcome set the status instead.
- **Dispute views stay role-scoped like job views.** `BuyerDisputeOut` never
  carries the clawback amount; `SellerDisputeOut` never carries the refund
  amount; only `AdminDisputeOut` carries both. `reason`/`status` are visible
  to all three (the seller has to know what they're accused of).
- **Seller→buyer reviews expose only the aggregate to the buyer.**
  `GET /v1/profile` returns `rating`/`rating_count`, never the individual
  `SellerReview` rows or comments; those stay admin-side (`GET
  /v1/admin/buyers` is the same aggregate, not a review list) until the
  moderation/abuse sub-phase decides whether buyers see more.

## Threat model (pilot)

Identity comes from an authenticated principal, never from a request body or
query param. Three roles: `buyer`, `seller`, `admin`, each resolved from an
`auth_sessions` row (see "Update — real-user auth" above) — not a shared
secret, not a caller-supplied claim. This is enough to give real users
distinct, unspoofable, revocable identities; the residuals above (no
rate-limiting, the reset-timing delta, verification gating nothing) are
named pilot-grade gaps, not silent ones. See `ROADMAP.md` for what's still
ahead (admin RBAC, OAuth/social login).

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
