# Notification Preferences — Design

**Date:** 2026-07-14
**Status:** Approved (maintainer pre-approved spec + plan and the recommended
option at each choice point, 2026-07-14, through to merge)
**Scope:** Trust & safety sub-phase 4 of 4 — the last one (disputes ✓ →
seller reviews ✓ → moderation ✓ → **notification preferences**).

## Goal

Let users mute the notification kinds they don't want, without ever muting
mail that records money movement. Per-kind control with a hard must-send
floor; enforcement at enqueue time so the notifications table remains "what
will actually send."

## Decisions (maintainer, 2026-07-14)

1. **Granularity:** per-kind opt-out with a must-send set.
2. **Must-send floor: money only** — `REFUND_ISSUED_BUYER`,
   `DISPUTE_RESOLVED_BUYER`, `DISPUTE_RESOLVED_SELLER`,
   `PAYOUT_FAILED_ADMIN`. The other ten kinds (offer nudge, three buyer
   job-lifecycle kinds, `JOB_CANCELLED_SELLER`, `DISPUTE_OPENED_SELLER`,
   and the four admin queue kinds) are mutable.
3. **Shape (approach A):** sparse `notification_mutes` table — a row means
   muted, absence means subscribed; new kinds in future forks default to
   subscribed with zero backfill. (B: JSON blob on User — schemaless
   validation, per-enqueue parsing; C: full user×kind matrix — backfill on
   every new kind. Both rejected.)
4. **Enforcement point (controller call, obvious-right-answer rule):**
   enqueue time. A muted kind never creates an outbox row; drain, retries,
   renderers, and the admin notifications list are untouched.

## Data model (migration #8)

New entity `NotificationMute` (table `notification_mutes`):

| column       | type                          | notes                    |
|--------------|-------------------------------|--------------------------|
| `id`         | UUID PK                       | default uuid4            |
| `user_id`    | String(128), indexed          |                          |
| `kind`       | `_enum(EventKind)`            |                          |
| `created_at` | tz timestamp                  | default now              |

`UNIQUE(user_id, kind)`. Sparse: row = muted.

In `notifications.py`, two module constants (explicit beats deriving from
name suffixes):

- `MUST_SEND: frozenset[EventKind]` — the four money kinds above.
- `KIND_ROLES: dict[EventKind, UserRole]` — every kind mapped to its
  recipient role (5 buyer, 4 seller, 5 admin). A test asserts the dict
  covers `set(EventKind)` exactly, so a future kind added without a role
  mapping fails fast (same pattern as the every-kind-has-a-renderer
  invariant test).

## Enforcement

`enqueue(session, kind, user_id, payload)` — after the existing missing-user
skip: if `kind not in MUST_SEND` and a `NotificationMute(user_id, kind)` row
exists → skip, logged at debug level (not warning — muting is normal
operation, not an anomaly). `enqueue_admins` applies the same per-admin
filter inside its loop (one query for the kind's muted admin ids, not one
per admin). Must-send kinds never consult the table — a smuggled DB mute row
for a money kind is ignored, enforced by test.

Nothing else in the pipeline changes.

## API

Both endpoints serve any authenticated role (buyer, seller, admin — the
`Principal` dep directly; admins have real per-admin mutes). NOT
suspension-gated: changing a preference is not an acquisition verb.

- `GET /v1/notification-preferences` → `list[NotificationPreferenceOut]`
  for the caller's role only, in `KIND_ROLES` declaration order:
  `{kind, muted: bool, locked: bool}` — `locked: true` for must-send kinds
  (always with `muted: false`).
- `PUT /v1/notification-preferences` body `{"muted": [kind, ...]}` —
  **replace-set semantics** for the caller's mutable kinds: rows in the
  list are muted, mutable kinds absent from the list are unmuted.
  422 when the list names a must-send kind, a kind outside the caller's
  role, or an unknown kind (FastAPI enum validation covers unknown).
  Duplicates in the list are tolerated (set semantics). Idempotent.
  Returns the same shape as GET. Concurrent PUTs: last-writer-wins;
  replace-set is implemented delete-then-insert in one transaction, with
  the same IntegrityError→409 guard shape as the other UNIQUE-backed
  writes should a race interleave (loser retries or accepts 409 — either
  way no 500 and no duplicate rows).

No admin-views-others'-preferences endpoint (YAGNI). No new notification
kinds. No digest/frequency settings.

## Testing

New `tests/test_notification_prefs.py`:

- GET defaults: all role kinds `muted: false`; money kinds `locked: true`;
  role scoping (buyer sees exactly the 5 buyer kinds, seller 4, admin 5).
- PUT: mute `offer_received` → matching enqueue creates NO row (drive a
  real offer via the matching flow and assert the outbox); unmute (PUT
  without it) → next offer enqueues again. Replace-set: PUT A then PUT B
  leaves only B muted.
- Must-send: PUT naming a money kind → 422 and no state change; a
  DB-smuggled mute row for `REFUND_ISSUED_BUYER` is ignored by enqueue
  (white-box test).
- 422s: off-role kind, unknown kind string.
- Admin: one admin mutes `report_opened_admin` → a filed report notifies
  the OTHER admins but not them (enqueue_admins filter).
- Invariant: `KIND_ROLES` covers every `EventKind`; `MUST_SEND ⊆
  KIND_ROLES`.
- PG-gated: two concurrent PUTs with different sets → no 500, final state
  equals one of the two sets, no duplicate rows.

Demo act 6: carol mutes `offer_received`; a new job matches her but no
offer mail is queued (assert via admin notifications list); she still sees
the offer via `GET /v1/seller/offers`; she unmutes; next offer mails again.

Docs: ROADMAP — T&S bucket COMPLETE (all four sub-phases; abuse
signals/limits stays deferred); SECURITY.md moderation-adjacent note (money
mail cannot be muted — the floor is server-side, not UI convention);
README endpoint lists; CLAUDE.md migration count → 8.

Suite gates unchanged: ruff, ruff format, pyright strict, SQLite + Postgres
full runs, demo exit 0, 8 migrations from scratch — bare exit codes.

## Non-goals

Digest/frequency/quiet-hours, per-channel prefs (email is the only
channel), admin management of others' preferences, retroactive suppression
of already-queued rows, unsubscribe links in mail bodies (needs signed
tokens — roadmap candidate with API hardening), new notification kinds.
