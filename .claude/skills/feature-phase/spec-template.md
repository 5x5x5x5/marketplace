# <Phase name> — design

**Date:** YYYY-MM-DD · **Status:** draft | approved · **Branch:** `<phase>`

## Goal

Two or three sentences: what changes for buyers, sellers, operators, and why
now.

## Decisions (maintainer-confirmed)

1. **<Decision>:** the rule, and the alternative that was rejected.
2. ...

## Data model (Alembic migration #N)

| Table | Fields |
|---|---|
| `<table>` | `id` UUID pk · ... (money `Numeric(12,2)`, enums non-native, datetimes tz-aware) |

## Provider calls and idempotency keys

Port method changes (fake + Stripe adapters). Every outbound key derived from
an id, distinct per operation: `<op>:{job_id}[:suffix]`.

## State transitions and locking

Which rows are locked, in what order (`Payment` before `Job`), where status is
re-checked after a lock-free window, which commits happen mid-handler and why.

## Endpoints and views

| Endpoint | Caller | Behavior / errors |
|---|---|---|

Which view family each response uses and which fields each role must never
see.

## Notifications

New `EventKind`s, recipient, role-safe payload contents.

## Settings

`name: type = default` (and `.env.example` lines).

## Error handling

Provider failure -> 502 and what stays recorded; webhook unknown ids / replays.

## Testing

Unit, API, Postgres race tests, live-server tests; the asymmetry assertions.
Suite green on SQLite and Postgres before done.

## Non-goals

## Constraints carried forward

Restate the `CLAUDE.md` invariants this phase leans on.
