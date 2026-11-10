# Capacity before and after the performance work (2026-11-10)

One before/after pair, measured with `loadtest/run_baseline.sh` and compared
with `loadtest/compare.py`. "Before" is the commit that added the harness,
with none of the performance changes. "After" is the tree with the pool
sizing, SQL pagination, bounded quote path, housekeeping moved off read
paths, and the idempotency middleware's database work moved off the event
loop.

## Conditions

Both runs used the same matrix, back to back, with nothing else running.

| | before | after |
|---|---|---|
| Commit | `3388eae` | `a267c25` |
| App | one uvicorn worker | one uvicorn worker |
| Database | local Postgres 16 | local Postgres 16 |
| Payment provider | fake (answers in about 0 ms) | fake |
| Seed | 50 sellers, 10 buyers, 100 listings (1,000 for the last stage) | same |
| Stage length | 30 s | 30 s |
| Machine | 4 cores, 16 GB, load generator on the same box | same |
| Pool settings | SQLAlchemy defaults (5 + 10 overflow, 30 s timeout) | new defaults (10 + 30 overflow, 5 s timeout) |

## Results

| Stage | Users | req/s | p50 | p95 | Failed |
|---|---:|---|---|---|---|
| browse | 10 | 79.2 → 77.2 | 130 ms → 130 ms | 180 ms → 180 ms | 0 → 0 |
| browse | 25 | 79.5 → 80.0 | 310 ms → 310 ms | 460 ms → 440 ms | 0 → 0 |
| browse | 50 | 75.2 → 81.9 | 650 ms → 600 ms | 1.1 s → 810 ms | 0 → 0 |
| browse | 100 | **0 → 64.4** | n/a → 1.0 s | n/a → 5.6 s | see below |
| lifecycle | 6 | 1.1 → 37.3 (before is contaminated, see below) | 34 ms → 20 ms | 28 s → 49 ms | 0 → 0 |
| lifecycle | 12 | 43.2 → 70.8 | 74 ms → 32 ms | 470 ms → 98 ms | 0 → 0 |
| lifecycle | 24 | 52.6 → 82.1 | 190 ms → 150 ms | 1.3 s → 290 ms | 0 → 0 |
| login | 5 | 17.5 → 17.5 | 280 ms → 280 ms | 340 ms → 340 ms | 0 → 0 |
| login | 10 | 17.5 → 17.1 | 570 ms → 580 ms | 690 ms → 700 ms | 0 → 0 |
| browse, 1,000 listings | 25 | 40.2 → 79.6 | 580 ms → 310 ms | 1.1 s → 450 ms | 0 → 0 |

## Reading the numbers

**The 100-user stall is gone.** Before, a burst of 100 concurrent browsers
completed zero requests in the 30-second stage. The server log has 70
`QueuePool limit ... reached` errors: every request was queued for a
connection and timed out at 30 seconds, after the stage had ended, so the
harness recorded neither a success nor a failure. After, the same burst
completed 1,868 requests. 17 of them (0.9%) failed, all within the same second
and all as `503` with `Retry-After`: the pool
shedding load on purpose rather than queueing. The server log has no pool
errors.

**The stall outlived its stage.** The first lifecycle stage in the "before"
run completed 31 requests with a 28-second p95 because the server was still
working through the previous stage's backlog. That row measures the
hangover, not lifecycle capacity at 6 users, so treat the before figure as
"unavailable", not as 1.1 req/s. The 12- and 24-user rows are clean
comparisons.

**The lifecycle got faster under load.** At 24 users, throughput rose from
52.6 to 82.1 req/s and p95 fell from 1.3 s to 290 ms. This is the quote path
(a fixed number of queries instead of two per candidate seller, no delete on
every quote) and housekeeping no longer running on every seller poll.

**Catalog cost no longer grows with the catalog.** With 1,000 listings, the
"before" throughput at 25 users was half its 100-listing figure (40.2 against
79.5 req/s). After, it is the same as with 100 listings (79.6 against 80.0),
because the listing endpoint pages in SQL.

**Unchanged, as expected.** Light browse load sits on the same plateau of
about 80 req/s: one Python process is the ceiling there and nothing in this
work raises it. Login is flat at about 17 per second: that is argon2 on four
shared cores, and the fix for it is a rate limit at the edge, which this
template leaves to the deployment (`docs/DEPLOY.md`).

## What this does not show

- **Provider latency.** The fake provider answers instantly. Accept,
  complete and cancel hold row locks and a pooled connection across the
  provider call (`ROADMAP.md`, known limitations), so real Stripe latency
  will lower lifecycle throughput. That coupling is not measured here.
- **Absolute capacity.** The load generator shared the four cores with the
  server and the database. The shapes (a stall that is gone, a cost that no
  longer grows) are the result; the absolute req/s are indicative.
- **Repeatability.** This is one pair of 30-second runs. The differences
  called out above are large; the single-digit percentage differences in the
  table are within what a second run would move.
- **More than one process.** One worker, one instance. `MAINTENANCE_ENABLED`
  and the pool arithmetic for replicas are documented but were not load
  tested.

## Reproducing

```
uv sync --group loadtest
DB_NAME=marketplace_load BROWSE_USERS="10 25 50 100" LIFECYCLE_USERS="6 12 24" \
  LOGIN_USERS="5 10" STAGE_SECONDS=30 SELLERS=50 BUYERS=10 CATALOG_SIZE=100 \
  SCALE_CATALOG_SIZE=1000 SCALE_USERS=25 bash loadtest/run_baseline.sh
python3 loadtest/compare.py loadtest/results/<before> loadtest/results/<after>
```

Run it on both commits with the same variables. The runner writes its
conditions next to its numbers; `compare.py` warns when they differ.
