# Load-test harness

Measures request capacity of the app *as configured*: one uvicorn process by
default, the default anyio threadpool, the default SQLAlchemy pool, real
Postgres (row locks are no-ops on SQLite, so SQLite numbers flatter every
`FOR UPDATE` path), and the deterministic fake payment provider. It exists to
compare two builds of the app under identical conditions, not to produce
absolute production numbers.

## Run

```sh
uv sync --group loadtest
bash loadtest/run_baseline.sh
```

The runner drops and recreates one Postgres database (`DB_NAME`, default
`marketplace_load`; the name must contain `load` unless `ALLOW_ANY_DB=1`),
migrates it, seeds it, starts the app, runs each stage headless, and writes to
`loadtest/results/<timestamp>/`:

| File | Contents |
|---|---|
| `<stage>_stats.csv`, `_stats_history.csv`, `_failures.csv`, `_exceptions.csv` | Locust's own CSVs, one set per stage |
| `<stage>.log` | Locust stderr for the stage |
| `conditions.json` | git commit, dirty flag, workers, catalog size, core count, RAM, pool/threadpool env overrides, Postgres and Locust versions, the stage matrix |
| `stages.jsonl` | per stage: users, seconds, load average before/after, cumulative jobs by status |
| `summary.json` | conditions plus one record per stage (req/s, p50/p95/p99, failures, per-endpoint rows, jobs created during the stage) |
| `summary.md` | the same as a Markdown table |
| `uvicorn.log` | server log (look here for pool-timeout errors) |

### Stage matrix and other knobs (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `BROWSE_USERS` / `LIFECYCLE_USERS` / `LOGIN_USERS` | `10 25 50` / `6 12 24` / `5 10` | user counts, one stage per number; an empty string skips that scenario |
| `STAGE_SECONDS` | `60` | duration of every stage |
| `SPAWN_RATE` | all users at once | users spawned per second; the default is a deliberate thundering herd |
| `SCALE_CATALOG_SIZE`, `SCALE_USERS` | unset, `25` | if set, reseed to at least that many listings and run one extra browse stage |
| `SELLERS`, `BUYERS`, `SELLER_CAPACITY` | `50`, `10`, `3` | seeded accounts |
| `CATALOG_SIZE` | `100` | minimum number of listings (spread evenly over sellers and the two service types) |
| `WORKERS` | `1` | uvicorn worker processes |
| `PORT` | `8123` | app port |
| `DB_NAME`, `DB_ADMIN_URL` | `marketplace_load`, local `marketplace` superuser on `/postgres` | database to recreate and the admin connection used to do it |
| `RESET_DB` | `1` | `0` reuses the database (seeding is idempotent) |
| `RESULTS_ROOT` | `loadtest/results` | output root |

Any environment variable with `POOL`, `ANYIO`, `UVICORN`, `THREADS` or
`WEB_CONCURRENCY` in its name is passed through to the app (the app reads
whatever settings it knows about) and recorded under `env_overrides`.

The runner also exports `ALLOW_UNSIGNED_WEBHOOKS=true` (fake-provider webhooks
are unsigned) and pins `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` and
`SMTP_HOST` empty, so a developer `.env` can never reach a real provider.

Quick smoke run (about a minute):

```sh
BROWSE_USERS="3" LIFECYCLE_USERS="4" LOGIN_USERS="2" STAGE_SECONDS=10 \
    SELLERS=8 BUYERS=3 CATALOG_SIZE=40 bash loadtest/run_baseline.sh
```

Individual scenarios, with a server and a token file from
`loadtest/seed_loadtest.py` already in place:

```sh
uv run locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8123 \
    -u 25 -r 25 -t 60s BrowseUser
```

## Comparing two runs

```sh
python3 loadtest/compare.py loadtest/results/<before> loadtest/results/<after>
```

Prints the conditions side by side and, per stage, `before -> after (change)`
for req/s, p50, p95 and failure rate. It warns when the two runs' conditions
differ. Stages are matched by label (`browse-25`, `lifecycle-12`, ...), so use
the same stage matrix for both runs. `python3 loadtest/summarize.py <dir>`
regenerates `summary.json`/`summary.md` from a directory's CSVs.

For a trustworthy comparison: same machine, nothing else running, same
`CATALOG_SIZE`/`SELLERS`/`BUYERS`/`STAGE_SECONDS`/`WORKERS`, and check the
`Load avg` column in `summary.md` (high before the stage starts means another
process was competing).

## Scenarios (select by user-class name)

| Class | What it exercises |
|---|---|
| `BrowseUser` | Anonymous reads: public catalog JSON (unfiltered, filtered by service type, detail, seller profile) and the server-rendered pages built from the same views (`/`, `/listings/{id}`, `/sellers/{id}`) |
| `LifecycleBuyer` + `LifecycleSeller` | Authenticated flow: quote (about 30% pinned to a catalog listing, the rest platform-dispatched) -> job -> offer poll -> accept (row locks held across the provider charge) -> complete (transfer and margin booking). Users run at 1 buyer to 2 sellers |
| `LoginUser` | argon2 logins with no rate limiting: pure CPU and memory pressure |

Domain outcomes expected under contention (409 capacity or margin floor, 410
consumed quote) are recorded as successes, so the failure column means
transport errors, 5xx, or unexpected 4xx only.

`LifecycleSeller` users each round-robin over `LOADTEST_SELLERS_PER_USER`
(default 5) seller accounts taken from the head of the seeded list, because the
default matching strategy sends offers to the longest-available sellers first.
`LOADTEST_PINNED_SHARE` (default 0.3) sets the pinned-quote fraction.

## Reading the results

- Compare shapes, not just values: a throughput plateau with p50 growing
  linearly with users is queueing at a fixed capacity; a stage where requests
  fail with 500 and p50 sits at about 30 s is connection-pool exhaustion
  (confirm in `uvicorn.log`).
- Lifecycle numbers are only meaningful if work flowed through. The summary's
  "Completed jobs" column is the number of jobs that reached `completed`
  during the stage; zero in a lifecycle stage means the funnel stalled and the
  req/s figure is not comparable.
- Job, session and quote tables grow across stages within one run, so stage
  order matters; keep it fixed for before/after.
- `loadgen_shared_box` is always `true`: Locust runs on the same machine as
  the app and the database. Numbers are indicative, not production truth.
- The fake payment provider answers in-process with about zero latency, so the
  accept and complete paths understate production behavior, where those row
  locks are held across real network calls.
- Login throughput is bounded by argon2 CPU cost across all cores, so it moves
  with `WORKERS` and with any other load on the box.
