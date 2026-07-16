# Try-out findings — 2026-07-16

Live-server findings from hand-driving the template over Swagger/curl
(Postgres, fake payment provider). Each entry: what surfaced, where it lives,
severity, status.

## F1 — Seller completion receipt leaks buyer_price and margin

- **Surfaced:** seller's `POST /v1/seller/jobs/{id}/complete` response showed
  `{"buyer_price": "20.00", "seller_payout": "14.00", "margin": "6.00"}`.
- **Where:** `api.py:1119` — `response_model=TransactionOut` (the admin ledger
  view) on a seller endpoint. `models.py:196` TransactionOut carries
  buyer_price/seller_payout/margin.
- **Severity:** Important/Critical-class — violates the core non-negotiable
  ("seller payloads never carry buyer_price"); the invisible spread IS the
  business model.
- **Why every review missed it:** predates the per-branch review culture
  (template-build era); later reviews were diff-scoped; the demo prints
  `tx['margin']` from the seller call, which normalized it; asymmetry tests
  cover job/offer views, mail payloads, and dispute views — never the
  completion receipt.
- **Fix shape:** seller-scoped completion view (`seller_payout`,
  `completed_at` only); TransactionOut stays admin-only
  (`GET /v1/admin/transactions`); demo reads margin from the admin summary;
  regression test asserting no seller response ever carries
  buyer_price/margin.
- **Status:** FIXED (branch fix-race-and-receipt). Completion endpoint now
  returns SellerCompletionOut (job_id/seller_payout/completed_at only);
  TransactionOut is admin-only; demo reads margin from the admin summary;
  regression test test_completion_receipt_is_seller_scoped.

## F2 — Session commit lands AFTER the response: read-your-writes race on every write endpoint

- **Surfaced:** gauntlet run — `POST /v1/jobs` 404 "quote not found" ~100ms
  after the quote's 200; the quote row was present and valid in Postgres
  moments later. Reproduced 2/30 in a tight loop.
- **Proven mechanism:** controlled experiment (fastapi 0.136 / starlette 1.0 /
  uvicorn): a yield-dependency teardown that sleeps finishes AFTER the client
  has the full response. `db.get_session` commits in teardown → every 2xx is
  sent before its transaction commits.
- **Blast radius:** any client that chains calls (mobile checkout, scripts,
  webhooks pointing back) can act on state that isn't committed yet —
  quote→job, signup→immediate-use, accept→complete. Worse: a commit that
  FAILS in teardown means the client got a 200 for work that never persisted.
- **Why 270 tests + demo never saw it:** TestClient awaits the entire app
  cycle (teardown included) before returning — the race is unobservable
  in-process, by construction. Only a real network client can see it.
- **Fix shape (own branch):** commit must happen before the response leaves —
  e.g. session-owning ASGI middleware/contextvar (commit on
  http.response.start for 2xx, rollback otherwise) or an APIRoute subclass
  committing after the handler, before response construction. Plus a
  regression test that talks to a REAL uvicorn over a socket (the only
  honest harness for this class).
- **Status:** OPEN.

### F2 addenda (from the 30-transaction gauntlet)

- Also manifested on job→cancel (fresh job 404s on immediate cancel) — it's
  every chained write, not just quote→job. Hit rate ~10-15% on localhost
  back-to-back calls.
- **F2b:** the idempotency middleware stores its replay record AFTER
  forwarding the response (`idempotency.py` — `record_send` streams to the
  client, the store happens after `self.app` returns, in its own session).
  Observed live: same-key immediate retry re-executed instead of replaying
  (got 404 where the first call got the job — the row locks kept it SAFE,
  but the idempotency contract didn't hold). Fix rides with F2: buffer,
  store, then send (responses are small JSON; streaming isn't used here).

## F3 — README dispute-endpoint line omits the /seller prefix (doc nit)

- README "Disputes" line reads "Seller: `GET /jobs/{id}/dispute`"; the real
  route is `GET /v1/seller/jobs/{id}/dispute` (api.py:965). The unprefixed
  path 403s a seller ("buyer credentials required") — misled the gauntlet,
  will mislead a fork developer. One-line doc fix.
- **Status:** OPEN (trivial).

## F4 — Creation endpoints disagree on 200 vs 201 (consistency nit)

- `POST /v1/auth/signup` and `POST /v1/jobs/{id}/dispute` return 201;
  `POST /v1/quotes` and `POST /v1/jobs` return 200. Cosmetic API-design
  inconsistency; a fork building a client will trip on it once.
- **Status:** OPEN (low; decide a convention and align, or document).

## Verified-good under the gauntlet (30 transactions)

Margins identity exact at every check; capacity saturation expires the
overflow job; declines expire single-seller jobs; admin-cancel refunds
(payment REFUNDED, fee still counted — by design); dispute views asymmetric
both directions; 0/0 dispute rejection; mute suppressed mail while the
in-app offer survived; offer TTL expired via the maintenance loop alone;
comment takedown hid text while ratings held; report filing counterparty
check enforced; no unexpected field leaks beyond known F1.

## F5 — Cancel-vs-complete race: admin cancel refunds a COMPLETED job (stale-relock trap)

- **Surfaced:** stress-test race R3 — admin cancel and seller complete fired
  simultaneously on an accepted job; BOTH returned 200. Final state:
  job cancelled + payment refunded, while the transaction row stayed booked
  (margin counted) and the payout stayed paid. The platform paid the seller
  AND refunded the buyer on the same job. Hit 1-for-1 on the first race
  attempt.
- **Sequential guard works** (repro: cancel after complete → 409
  "cannot cancel a completed job"), so this is purely a race.
- **Mechanism — the documented stale-relock trap, in code that predates the
  lesson:** `admin_cancel_job` (api.py) does an unlocked
  `session.get(Job, ...)` for existence, which loads the Job into the
  session's identity map; the later `session.get(..., with_for_update=True)`
  acquires the row lock but (expire_on_commit=False) returns the CACHED
  object with the pre-race ACCEPTED status. The 409 guard reads stale state,
  passes, and the handler refunds + overwrites COMPLETED with CANCELLED.
  The disputes branch documented exactly this
  (.superpowers/sdd/progress.md LESSON; feedback memory): re-read with
  populate_existing/refresh after any lock that follows an earlier load.
- **Same shape elsewhere:** buyer `cancel_job` (unlocked ownership peek →
  locked re-get → status check) — its race is accept-vs-cancel; and audit
  `_sweep_stale_payments`'s re-lock/re-check for the same staleness.
  `complete_job` is safe (its FIRST touch is the locked get — nothing stale
  to return).
- **Why the existing PG race test (cancel-vs-webhook, 20/20 green) missed
  it:** that race crosses AWAITING_PAYMENT→ACCEPTED — both statuses pass the
  cancel guard, so a stale read changes nothing observable. Only
  cancel-vs-COMPLETE crosses into the forbidden set.
- **Fix shape:** `populate_existing=True` on the locked re-gets (or
  `session.refresh` post-lock) in both cancel paths + sweep audit; PG race
  regression test for cancel-vs-complete asserting the pair never lands
  (cancelled+refunded) with a booked transaction/paid payout.
- **Severity:** Critical (double-pay under a realistic race).
- **Status:** FIXED (branch fix-race-and-receipt). populate_existing=True on
  the locked re-gets in both cancel paths AND the stale-payment sweep;
  PG race test test_cancel_vs_complete_race (5 attempts); verified live 25
  races, 0 double-wins, 0 incoherent money (was 1/1 pre-fix).

### Stress-run notes (100 threaded lifecycles, 5 buyers / 4 tiered sellers)

Surge pricing came alive (rideshare 20.00→50.00 across the burst; cleaning
flat 80.00 as configured); re-match walked the candidate pool (offer/expired
rows spread across all five sellers); money identity held at every check
(61 transactions); F2 needed retries on 4/102 chained calls; R1 double-accept
correctly split 200/409; R2 parallel same-key create: safety held (exactly
one job per quote) but the loser got a 404 instead of a replay — the
idempotency middleware's admitted concurrent race, same family as F2b. The
"52 offerless pending jobs" in the raw output were a script artifact (offers
had gone to the seed seller the script didn't poll).
