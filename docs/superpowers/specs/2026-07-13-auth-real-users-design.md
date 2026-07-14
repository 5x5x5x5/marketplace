# Auth: real users — design

**Date:** 2026-07-13 · **Status:** approved

## Goal

Replace the pilot HMAC tokens with a real user store: signup, login, revocable
sessions, password reset, and email verification — so a fork can pilot with
hundreds of self-service users instead of hand-minted tokens. This is the last
gap that makes a pilot impossible.

## Decisions (confirmed with Danny)

1. **Separate buyer and seller accounts.** One account carries exactly one
   role; acting as both sides means two accounts. Tokens keep single-role
   claims; the same email may register once per role (unique on
   `(email, role)`). Admin accounts are seeded, never self-signup.
2. **Email flows in scope via a stubbed port.** Verification and password
   reset ship now against an `EmailSender` port with a console/dev adapter
   that logs instead of sending. Forks plug in SES/Resend/Postmark; the port
   seeds the future notifications phase.
3. **DB-backed opaque sessions.** Login issues a random opaque token; the DB
   stores only its sha256. Logout/ban/reset revoke by deleting rows. One
   indexed lookup per request.
4. **Hand-rolled on existing machinery.** No fastapi-users (maintenance
   mode), no Supabase (breaks local-first tests). One new dependency:
   `pwdlib[argon2]` for password hashing.

## Data model (Alembic migration #3)

| Table | Fields |
|---|---|
| `users` | `id` UUID pk · `email` String(320) · `role` enum buyer/seller/admin (non-native, as elsewhere) · `password_hash` String(256) · `display_name` String(128) · `email_verified` bool default False · `created_at`/`updated_at` · **UniqueConstraint(email, role)** |
| `auth_sessions` | `id` UUID pk · `user_id` FK users · `token_hash` String(64) unique indexed (sha256 hex of the bearer) · `created_at` · `expires_at` |
| `email_tokens` | `id` UUID pk · `user_id` FK users · `purpose` enum verify/reset · `token_hash` String(64) unique · `expires_at` · `used_at` nullable (single-use) |

Domain identity: `str(user.id)` becomes the principal `sub`, slotting into the
existing string-keyed `jobs.buyer_id`, `seller_profiles.id`, etc. — no
migration of existing domain rows. `BuyerProfile`/`SellerProfile` stay the
domain records (capacity, ratings, payments_ready); `User` is identity only,
created together with its profile row at signup.

## Auth mechanics

- **Hashing:** argon2 via `pwdlib`. One module-level `PasswordHash` instance.
- **Tokens:** `secrets.token_urlsafe(32)`; only sha256 stored. A DB leak never
  yields usable bearers.
- **The principal seam does not move.** `current_buyer` / `current_seller` /
  `require_admin` keep their signatures; internally: bearer → sha256 → session
  join user → expiry check → `(role, sub=user_id)`. No endpoint outside
  `/v1/auth` changes.
- **Admin bootstrap:** `ADMIN_EMAIL` + `ADMIN_PASSWORD` settings; lifespan
  creates the admin user if absent. Empty settings → no admin created (and a
  startup log line says so).
- **Session TTL:** `SESSION_TTL_HOURS` setting (default 72). Expired session
  rows are deleted by the existing lazy `_sweep` (one more rule) — same
  pattern as offers/payments.
- **EmailSender port** (`src/marketplace/email/port.py`):
  `send(to: str, subject: str, body: str) -> None` protocol +
  `ConsoleEmailSender` adapter (logger.info). `get_email_sender()` dependency;
  selection by future setting, console-only for now. Links built from
  `BASE_URL` setting.

## Endpoints (`/v1/auth`)

| Endpoint | Behavior |
|---|---|
| `POST /signup` | `{email, password, role: buyer\|seller, display_name}` → create User + profile row, send verification email, **return a session immediately** `{token, expires_at, user}`. Duplicate (email, role) → 409. Role admin → 422. |
| `POST /login` | `{email, password, role}` (role required — same email may own both accounts) → `{token, expires_at, user}`. Uniform 401 for bad email OR password. |
| `POST /logout` | Authenticated; deletes the current session row. |
| `GET /me` | `{id, email, role, display_name, email_verified}`. |
| `POST /verify` | `{token}` → sets `email_verified`, marks token used. Invalid/expired/used → 400. |
| `POST /password-reset/request` | `{email, role}` → **always 200** (no enumeration); sends reset token if the account exists. |
| `POST /password-reset/confirm` | `{token, new_password}` → rehash, mark token used, **revoke all the user's sessions**. |

Password policy: min length 8, max 128 (bcrypt-style truncation doesn't apply
to argon2, but bound the input). No complexity rules (NIST guidance).

## What dies

`mint_token`, `_verify`, the HMAC helpers, and the `MARKETPLACE_SECRET`
setting — the entire pilot auth path is deleted. One trust mechanism remains.
The idempotency middleware's `peek_principal` (which verified HMAC statelessly)
is reimplemented over the sessions table: the middleware already opens its own
short `SessionLocal` for the idempotency lookup, and the principal resolution
joins `auth_sessions` in that same session — still best-effort, still returns
`None` on anything invalid so endpoints produce the real 401.
SECURITY.md's pilot-HMAC threat model is rewritten around sessions. The
"identity from the authenticated principal, never a request body" invariant is
unchanged — the seam it lives behind didn't move.

## Deliberate pilot-grade postures (documented, not built)

- **No login rate limiting** — stays with the existing gateway-deferral
  stance (SECURITY.md notes credential-stuffing exposure until then).
- **Email verification gates nothing yet** — the console adapter can't
  deliver real mail; forks flip on gating (e.g. unverified sellers can't post
  availability) when they wire a real sender. The gate point is noted in code.
- **No OAuth/social login** — fork concern, port-shaped hole documented.

## Testing

- **Zero churn in existing tests:** the conftest `auth(role, sub)` fixture
  keeps its interface but inserts a User (email `{sub}@test.local`, one
  precomputed shared argon2 hash constant — never hash per test) + AuthSession
  row directly and returns the bearer header. The fixture is idempotent per
  (role, sub) within a test: repeated calls reuse the same user and session. The `admin` fixture seeds an
  admin user the same way.
- **New `tests/test_auth.py`:** signup→login→logout lifecycle · wrong
  password 401 · duplicate (email, role) 409 · same email across roles OK ·
  expired session 401 · logout revokes (subsequent call 401) · full reset
  flow via a recording email adapter (captures the token from the port, not
  log-scraping) · reset revokes all sessions · verify flow + single-use ·
  enumeration resistance (reset request for unknown email still 200) · admin
  bootstrap on startup · signup cannot create admin.
- Migration #3 applies from scratch on SQLite and Postgres.

## Also updated

`scripts/demo.py` (signup/login replaces minting) · README (auth section +
endpoint map) · CLAUDE.md (invariant: DB sessions are the only trust path;
never reintroduce token minting or a second verifier) · `.env.example`
(`ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SESSION_TTL_HOURS`, `BASE_URL`) ·
ROADMAP (auth → done; OAuth + rate limiting noted).

## Constraints carried forward

uv · ruff + ruff format · pyright strict · SQLite-default tests / Postgres via
`DATABASE_URL` (fake-provider pinning in conftest stays) · ORM never leaves
the API layer · pricing/matching core untouched · Decimal money untouched.
