---
name: tryout
description: Try the marketplace out against a real running server - seed it, start uvicorn, drive the full quote-job-offer-accept-complete lifecycle over HTTP with the bundled probe, poke races and edge cases by hand, and record findings in the docs/tryout-findings format. Use before merging a phase that changes the lifecycle, money, auth, or pages, and whenever a bug might only show up outside TestClient.
---

# The live try-out gauntlet

Why it exists: the in-process suite (`TestClient`) awaits the whole app cycle,
dependency teardown included, before it returns, so it cannot see anything
that depends on what a real client receives and when. The first try-out
(`docs/tryout-findings-2026-07-16.md`) found, after 270 green tests and
clean reviews: F1, the seller receipt leaking buyer price and margin; F2,
every 2xx sent before its commit (a fresh quote 404'd on the next call 2/30
times); F2b, idempotency replays stored after the response; F5, a
cancel/complete race that paid the seller and refunded the buyer.

## 1. Start a server on a scratch database

Don't bind port 8000 if a developer server may be on it; pick another.

```bash
export DATABASE_URL=sqlite+pysqlite:////tmp/tryout.db   # or a scratch Postgres DB (better for races)
export ADMIN_EMAIL=admin@marketplace-demo.dev ADMIN_PASSWORD=try-it-admin-password
uv run uvicorn marketplace.api:app --port 8765          # run in the background
uv run python scripts/seed.py                            # service types, demo buyer/seller, tokens
```

Postgres is the honest engine for races (SQLite serializes writers). With
`STRIPE_SECRET_KEY` unset the fake provider is selected; it accepts unsigned
JSON on `POST /v1/payments/webhook`, so you can replay events by hand.

## 2. Run the probe

```bash
uv run python .claude/skills/tryout/probe.py --base-url http://127.0.0.1:8765 --iterations 30
```

It creates its own service type and a fresh buyer and seller (so offers can't
go to the seeded seller, an artifact that confused an earlier stress run),
then checks, over real HTTP: chained calls see committed state (F2), an
immediate same-key retry replays byte-identical (F2b), no buyer response
carries `seller_payout`/`margin` and no seller response carries
`buyer_price`/`margin`/`buyer_id` (F1), the landing page source has no
forbidden strings, and every booked transaction's margin equals buyer price
minus payout as each side saw them. Exit 1 means findings, printed in the
findings-doc shape. A setup call that 401s or 404s on something the previous
call just created is itself the F2 class.

## 3. Then go by hand, where the change is

The probe covers the happy path. Spend the rest of the time on what the
phase changed, with curl or `/docs` (Swagger has bearer auth wired):

- **Races:** fire the two conflicting calls at once (cancel vs complete,
  double accept, two resolves, cancel vs webhook) from threads, 20+ times;
  the invariant is "never both 200 with incoherent money".
- **Chained calls** with no pause between them for every new write endpoint.
- **Every new response, read as the other role and as anonymous.**
- **Edge inputs:** 0 and boundary amounts, expired windows, replayed
  webhooks, an oversized or chunked body with an `Idempotency-Key`.
- **The docs themselves:** follow the README's commands literally (F3 was a
  README route missing its `/seller` prefix).

## 4. Record findings

Append to the current findings doc (or start `docs/tryout-findings-YYYY-MM-DD.md`)
one entry per defect, in this shape:

```markdown
## Fn — <what's wrong, in one line>

- **Surfaced:** the exact call and response that showed it, and how often.
- **Where:** file:line or endpoint.
- **Severity:** Critical / Important / nit, and why.
- **Why it was missed:** which test or review couldn't see it.
- **Fix shape:** the approach, and the regression test that would catch it.
- **Status:** OPEN | FIXED (branch, mechanism, test names) | DEFERRED (reason).
```

Also record what was **verified good** and stress-run notes (counts, rates),
so the next try-out knows what was already exercised. Fix Critical findings
on their own branch with a test that fails without the fix (a real-socket
test from `tests/test_live_server.py` for ordering bugs), then flip the entry
to FIXED.

Stop the server when done.
