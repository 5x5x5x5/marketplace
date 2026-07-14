# Notifications — design

**Date:** 2026-07-14 · **Status:** approved · **Branch:** `notifications`

## Goal

Email the right party at each lifecycle moment — above all the seller's
new-offer email (a 2-minute offer TTL is unlivable with polling) and the
buyer's payment-due email (an unnoticed `AWAITING_PAYMENT` job dies by sweep
in 30 minutes) — delivered reliably, without new dependencies, and closing the
ROADMAP background-scheduler item along the way.

## Decisions (confirmed with Danny)

1. **Core lifecycle event set** — seven kinds (below); kitchen-sink extras
   (payout receipts, review nudges, digests) deferred.
2. **Transactional outbox + in-process drainer.** Domain transitions write a
   `notifications` row in the SAME transaction; a lifespan-spawned asyncio
   loop drains pending rows through the mail port. Exactly-once intent:
   rolled-back events never mail; committed events eventually always do.
3. **Stdlib SMTP adapter** (`smtplib` + `email.message`) alongside the
   console adapter — real deliverability the day a fork sets `SMTP_HOST`,
   no vendor pick, no new deps. Console stays the default.
4. **No preferences.** Everything here is transactional mail; opt-outs arrive
   with digest/marketing mail (ROADMAP note).
5. **Drainer is in-process** (asyncio task, `asyncio.to_thread` for sync DB
   work), claiming rows with `FOR UPDATE SKIP LOCKED` — multi-worker-safe on
   Postgres, serialized on SQLite. The same loop ticks the existing `_sweep`
   periodically — **this closes the ROADMAP background-scheduler item**; the
   lazy on-read sweeps stay. An external-worker extraction needs no schema
   change if a fork ever wants it.

## Data model (Alembic migration #4)

`notifications` table: `id` UUID pk · `user_id` FK users · `email` String(320)
(recipient snapshot at enqueue) · `kind` enum (non-native, as elsewhere) ·
`payload` JSON (role-safe snapshot, see below) · `status` enum
pending/sent/failed, indexed · `attempts` int default 0 · `next_attempt_at`
timestamp default now, indexed · `created_at` · `sent_at` nullable ·
`last_error` String(512) nullable.

## Module: `src/marketplace/notifications.py`

The whole feature in one focused file:

- **`EventKind` / `NotificationStatus`** StrEnums (live in `models.py` with
  the other domain enums; entities import them).
- **`enqueue(session, kind, user_id, payload) -> None`** — INSERT in the
  caller's transaction; looks up the recipient User for the email snapshot;
  missing user → skip + log (never breaks a money transaction).
  `enqueue_admins(session, kind, payload)` fans out to all ADMIN users;
  none → skip + log.
- **Renderer registry**: `RENDERERS: dict[EventKind, Callable[[dict], tuple[str, str]]]`
  — pure payload → (subject, body). Bodies are plain text.
- **`drain_once(mail: EmailSender, limit: int = 20) -> int`** — own short
  `SessionLocal`; claims `status == PENDING and next_attempt_at <= now` with
  `with_for_update(skip_locked=True)`, renders and sends per row; success →
  SENT + `sent_at`; any exception → `attempts += 1`, `last_error`,
  `next_attempt_at = now + 30s * 2^attempts`; `attempts >= NOTIFY_MAX_ATTEMPTS`
  (5) → terminal FAILED. Per-row try/except — one bad row never blocks the
  queue. Returns sent count.
- **The loop lives in `api.py`, not here** (it ticks `_sweep`, which lives in
  `api.py`, and `api.py` imports this module for the emitters — the loop in
  this module would be a circular import). `async def _maintenance_loop()`
  next to the lifespan: every `NOTIFY_DRAIN_SECONDS` (5) run
  `asyncio.to_thread(drain_once, get_mail_sender())`; every
  `SWEEP_INTERVAL_SECONDS` (60) run the existing `_sweep` (fresh session +
  `get_provider()`) in a thread. Each tick wrapped in try/except-log — a bad
  tick never kills the loop. Lifespan creates the task and cancels it on
  shutdown (suppressing `CancelledError`). Import direction stays one-way:
  `api → notifications → (mail, db, entities, models)`.

Tests never meet the loop: the `client` fixture doesn't enter the lifespan
context, so tests call `drain_once()` deterministically.

## Event kinds, emitters, payloads

Recipients are looked up by domain ids — post-auth these ARE user ids.
**Information asymmetry is enforced at enqueue**: payloads are built
role-safe inside the transaction (seller payloads never contain
`buyer_price`; buyer payloads never contain `seller_payout`), so the drainer
never re-queries mutable state.

| Kind | Recipient | Emitted from | Payload (all values JSON-safe strings) |
|---|---|---|---|
| `OFFER_RECEIVED` | seller | `_create_offer` | job_id, service_type_id, seller_payout, expires_at |
| `JOB_ACCEPTED_BUYER` | buyer | `accept_offer` | job_id, service_type_id, buyer_price, awaiting_payment bool (renders the payment-due line + 30-min warning only on the real-Stripe path) |
| `JOB_COMPLETED_BUYER` | buyer | `complete_job` | job_id, service_type_id, buyer_price |
| `JOB_EXPIRED_BUYER` | buyer | `_match_and_offer` EXPIRED branch (creation + re-match exhaustion) and `_sweep_stale_payments` (payment timeout) | job_id, service_type_id, reason ("no seller available" / "payment window elapsed") |
| `JOB_CANCELLED_SELLER` | seller | both cancel endpoints, when `job.seller_id` is set | job_id, service_type_id, seller_payout |
| `REFUND_ISSUED_BUYER` | buyer | cancel endpoints when `_release_payment` refunded | job_id, buyer_price |
| `PAYOUT_FAILED_ADMIN` | all admins | `complete_job` failure branch; `_apply_payment_event` transfer_failed | job_id, payout_id, seller_id, amount |

A buyer canceling their own job does not email the buyer.

## Mail adapter (in `src/marketplace/mail.py`)

`SmtpEmailSender(host, port, username, password, starttls, from_addr)` —
stdlib `smtplib.SMTP` + `email.message.EmailMessage`; STARTTLS then login
when credentials are set. Selection in `get_mail_sender()`: `SMTP_HOST` set →
SMTP, else console. New settings: `smtp_host=""`, `smtp_port=587`,
`smtp_username=""`, `smtp_password=""`, `smtp_starttls=True`,
`mail_from="marketplace@localhost"`, plus `notify_drain_seconds=5`,
`notify_max_attempts=5`, `sweep_interval_seconds=60`.

## Admin surface

- `GET /v1/admin/notifications?status=` — paginated `NotificationOut`
  (id, user_id, email, kind, status, attempts, last_error, created_at,
  sent_at). The ops answer to "did the seller ever get that email."
- `POST /v1/admin/notifications/drain` — manual `drain_once` + audit row.

## Error handling

- Enqueue failure fails the domain transaction — outbox exactness cuts both
  ways, and an INSERT failing means the DB is in real trouble anyway.
- Render/send failures are per-row: recorded, backed off, terminal FAILED
  after max attempts, inspectable via the admin endpoint. The drainer loop
  itself is crash-proof (tick-level try/except-log).
- The SMTP adapter wraps connection/send errors into exceptions the drainer's
  per-row handling absorbs — a mail outage backs off the queue, never the API.

## Testing

Deterministic via `drain_once` + the existing `mail_outbox` fixture:
asymmetry proofs (seller body contains payout, never buyer price; buyer body
never contains payout) · accepted with/without the payment-due line · both
expiry reasons · cancel-after-accept informs the seller · refund mail on
admin cancel · payout failure reaches all admins, skips-with-log when none ·
exploding sender → attempts/backoff/terminal FAILED progression · drain twice
→ one send (replay safety) · PG-gated concurrent drain (two threads,
SKIP LOCKED, no double-send) · `SmtpEmailSender` against a monkeypatched
`smtplib.SMTP` recorder (stdlib `smtpd` is gone in 3.12; no network) · admin
endpoints (list filter + manual drain + audit). Migration #4 applies from
scratch on SQLite and Postgres. **Suite must be run on BOTH backends before
the branch is declared done** (lesson from the auth build's FK incident).

## Also updated

`scripts/demo.py` (notifications act: create → `drain_once` → print the
console-sent mail) · README (notifications + SMTP config + the loop) ·
CLAUDE.md invariants (notify only via `enqueue` inside the domain
transaction; sends happen only in the drainer; payloads role-safe at
enqueue) · `.env.example` (SMTP block + drain/sweep intervals) · ROADMAP
(notifications AND background scheduler → Done; preferences/digests noted
under trust & safety).

## Constraints carried forward

uv · ruff + ruff format · pyright strict · SQLite-default tests / Postgres
via `DATABASE_URL` · zero new dependencies · ORM never leaves the API layer ·
pricing/matching core untouched · identity from principal only · never gate
on piped test output.
