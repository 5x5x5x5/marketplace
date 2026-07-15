# Moderation & Abuse — Design

**Date:** 2026-07-14
**Status:** Approved
**Scope:** Trust & safety sub-phase 3 of 4 (disputes ✓ → seller reviews ✓ → **moderation/abuse** → notification preferences).

## Goal

Give admins enforcement tools (suspend users, hide abusive content) and give
users a way to surface abuse (reports). No automation: resolving a report
never auto-suspends or auto-hides — admins wield the tools explicitly and the
report is the paper trail. Everything is audited; nothing is ever deleted.

## Decisions (Danny, 2026-07-14)

1. **Scope:** user suspension + content takedown + reporting pipeline.
   **Deferred to roadmap:** abuse signals/limits (automatic counters,
   thresholds, rate heuristics — fork-specific).
2. **In-flight money:** freeze-new / finish-in-flight. Suspension gates
   **acquisition verbs**, not login: suspended users still authenticate and
   may complete already-accepted work; suspension itself never moves money.
   Admin cancels specific jobs with the existing cancel/refund machinery if
   needed.
3. **Reporting eligibility:** counterparties only — you can report a user
   only if you share at least one assigned job with them, and a review only
   if you are a party to it (author or subject). Structural spam control.
4. **Shape (approach A):** state columns + one `reports` table + explicit
   per-endpoint guards. No moderation-event ledger (audit log covers
   provenance); no auth-dependency enforcement (an allowlist hidden in auth
   code is where the next endpoint gets forgotten).

## Data model (migration #7)

`User` (entities.py:318) gains:

| column             | type                              | notes            |
|--------------------|-----------------------------------|------------------|
| `status`           | enum ACTIVE/SUSPENDED, indexed    | default ACTIVE   |
| `suspended_reason` | String(2000) nullable             |                  |
| `suspended_at`     | tz timestamp nullable             |                  |

`Review` and `SellerReview` each gain `comment_hidden: bool = false`.

New entity `Report` (table `reports`), shaped like `Dispute`:

| column            | type                                   | notes                       |
|-------------------|----------------------------------------|-----------------------------|
| `id`              | UUID PK                                |                             |
| `reporter_id`     | String(128), indexed                   |                             |
| `target_kind`     | enum USER/REVIEW/SELLER_REVIEW         |                             |
| `target_id`       | String(128)                            | user id or str(review UUID) |
| `reason`          | String(2000), required (min 1)         |                             |
| `status`          | enum OPEN/ACTIONED/DISMISSED, indexed  | default OPEN                |
| `resolution_note` | String(2000) nullable                  |                             |
| `created_at`      | tz timestamp                           |                             |
| `resolved_at`     | tz timestamp nullable                  |                             |

`UNIQUE(reporter_id, target_kind, target_id)` — one report per reporter per
target, ever. A duplicate (sequential or concurrent) is a 409 via the same
IntegrityError-guard pattern the review endpoints use.

Enum storage follows the existing `_enum(...)` non-native pattern. New
`EventKind.REPORT_OPENED_ADMIN` for the admin notification.

## Suspension

- `POST /v1/admin/users/{user_id}/suspend` body `{reason: 1..2000}` → 404
  unknown user; 422 if target is an admin (admins cannot be suspended); 409
  already suspended. Sets status/reason/timestamp. Audited.
- `POST /v1/admin/users/{user_id}/reinstate` → 404 unknown; 409 not
  suspended. Clears reason/timestamp. Audited.
- Sessions are NOT revoked and login is NOT blocked — enforcement is
  verb-level, so revocation adds nothing (decision 2).

**Enforcement:** helper `_require_active(session, user_id)` — reads the User
row, raises 403 `"account suspended"` when SUSPENDED. Called at the top of
exactly these acquisition endpoints:

| gated (403 when suspended)          | still allowed while suspended        |
|-------------------------------------|--------------------------------------|
| buyer: create_quote, create_job     | login, logout, all GETs              |
| buyer: review_job, open_dispute     | buyer: cancel_job (exit verb)        |
| seller: set availability            | seller: complete_job (finish verb)   |
| seller: accept_offer                | seller: decline_offer (exit verb)    |
| seller: onboard_payments            | webhooks, admin acting on the user   |
| both: file report (`POST /v1/reports`) |                                   |

**Matching exclusion:** the seller-candidate query in `repo.py` excludes
sellers whose User row is SUSPENDED (anti-join / NOT EXISTS on
`users.status == SUSPENDED` — a profile without a User row stays eligible,
so white-box fixtures and the semantic "absence of identity ≠ suspended"
both hold). Suspended sellers stop receiving offers even with availability
rows and capacity; lazy re-match on decline/timeout flows through the same
query, so it is covered by construction.

## Content takedown

- `POST /v1/admin/reviews/{kind}/{review_id}/hide` and `/unhide`, `kind` ∈
  `buyer` (the `reviews` table) / `seller` (`seller_reviews`) → 404 unknown
  review; 409 already in the requested state. Sets `comment_hidden`. Audited.
  The row, the rating, and the profile aggregates are untouched — only the
  comment text disappears from non-admin serializations.
- **New admin read surfaces** (takedown needs a working surface; none exists
  today): `GET /v1/admin/reviews/{kind}` → newest-first list including
  `comment` and `comment_hidden`. No pagination (matches the other admin
  lists; rides the existing unpaginated-admin-lists roadmap note).
- **Invariant:** any non-admin serialization of a review renders `comment` as
  null when `comment_hidden` — structural (an entity-level `public_comment`
  property that Out-schemas read), not per-endpoint logic. Today no non-admin
  endpoint returns a stored review after creation (creation responses cannot
  be hidden yet by construction), so this invariant is future-proofing at
  near-zero cost, not dead code: the property is the single place the rule
  lives.
- `POST /v1/admin/users/{user_id}/reset_display_name` → sets
  `display_name = "user-" + user_id[:8]`. 404 unknown user. Audited.

## Reporting

- `POST /v1/reports` (buyer or seller bearer), body
  `{target_kind, target_id, reason}` → 201 `ReportOut`.
  Guard order: gated by `_require_active` → 404 target does not exist →
  422 self-report (USER target only: target user id equals reporter id) →
  403 not a counterparty → 409 duplicate (UNIQUE backstop, race-safe).
  - USER target eligibility: at least one Job exists with
    `(buyer_id=reporter, seller_id=target)` or the reverse (any status —
    the pair was matched). Self-report → 422.
  - REVIEW/SELLER_REVIEW target eligibility: reporter is the review's
    author or subject. (Reporting a review you authored is allowed — a
    "please take my comment down" request.)
  - *(Amended at final review.)* Subject-side report rights require
    discoverability the original spec forgot to provide: no participant
    surface exposed review ids. Added `GET /v1/jobs/{id}/reviews` (buyer)
    and `GET /v1/seller/jobs/{id}/reviews` (seller) — party-guarded 404,
    `JobReviewOut {id, kind, rating, comment, created_at}` with NO party
    ids (identity asymmetry holds), `comment` via `public_comment` (hidden
    stays hidden even to the parties), `kind` values equal to the report
    `target_kind` strings so the pair is directly reportable. This
    supersedes the takedown section's "no non-admin endpoint returns a
    stored review after creation" observation.
  - On create: `notifications.enqueue_admins(EventKind.REPORT_OPENED_ADMIN,
    {report_id, target_kind, target_id, reason})` — mirrors dispute-opened.
- `GET /v1/reports` — reporter's own reports: `id, target_kind, target_id,
  reason, status, created_at`. No `resolution_note` (admin-side prose stays
  admin-side, consistent with the dispute view asymmetry).
- `GET /v1/admin/reports?status=` — full rows, newest first, optional status
  filter (422 on unknown status value).
- `POST /v1/admin/reports/{report_id}/resolve` body
  `{status: actioned|dismissed, note?: ≤2000}` → 404 unknown; 409 not OPEN
  (resolutions are terminal). Sets status/note/resolved_at. Audited. Does
  NOT auto-suspend or auto-hide (decision: reports are paper trail; tools
  are explicit).

## View schemas

- `ReportOut` (reporter view): id, target_kind, target_id, reason, status,
  created_at.
- `AdminReportOut`: all columns.
- `AdminReviewOut` / `AdminSellerReviewOut`: full row incl. both party ids,
  `comment`, `comment_hidden` (admin sees identity; that asymmetry rule
  binds seller/buyer-facing views only).
- Suspension state stays on User: it is surfaced by the suspend/reinstate
  responses and the audit log, and to the suspended user only as the 403
  detail. `GET /v1/admin/buyers` is unchanged. (An admin users list is out
  of scope — admins suspend from reports/disputes context where the user id
  is at hand.)

## Testing

New `tests/test_moderation.py`:

- Suspension: lifecycle (suspend → 409 double-suspend → reinstate → 409
  double-reinstate → re-suspend); 404 unknown; 422 admin target; each gated
  verb → 403 while suspended; each allowed verb (complete, decline, cancel,
  login, GETs) verified working while suspended; matching exclusion (a
  suspended available seller gets no offer; job stays PENDING or goes to
  another seller); reinstate restores offers (re-match path).
- Takedown: hide → admin list shows comment + flag; unhide; 409s; 404s;
  aggregates and rating unchanged by hide; `public_comment` property
  nulls when hidden (unit test — the invariant's single home).
- Display-name reset: value shape, audit row, 404.
- Reports: eligibility matrix (counterparty user OK / stranger 403 /
  self 422 / review author OK / review subject OK / unrelated review 403 /
  unknown target 404); duplicate 409; PG-gated concurrent duplicate 409;
  resolve flow (actioned + dismissed, terminal 409, unknown-status 422);
  reporter view omits resolution_note; admin filter; admin notification
  enqueued on create; suspended reporter cannot file (403).
- Audit: one row per admin action, correct actor/action/target.

Demo act: seller files a report on a buyer review → admin lists reports →
hides the comment → suspends the buyer (gated verb 403 shown) → reinstates →
resolves the report ACTIONED. Docs: ROADMAP (this lands in Done; remaining
T&S = notification preferences), SECURITY.md (moderation asymmetries + the
suspension model), README endpoints, CLAUDE.md if it enumerates.

Suite gates unchanged: ruff, ruff format, pyright strict, SQLite + Postgres
full runs, demo exit 0, 7 migrations from scratch — all bare exit codes.

## Non-goals

- Abuse signals/limits, rate limiting (API-hardening phase), auto-actions on
  report resolution, report appeals, un-resolving reports, admin users list,
  deleting any row, revoking sessions on suspension, notifying the reported
  party, pagination.
