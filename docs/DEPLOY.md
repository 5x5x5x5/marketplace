# Deploying and scaling

A vendor-neutral guide to running this app somewhere real: what it needs,
how to size it, what to put in front of it, and what to check before taking
real payments. It describes this repository's behavior; settings and
defaults are from `src/marketplace/settings.py` and `.env.example`. Nothing
here requires a particular hosting provider.

This template has not been operated in production from this repository.
Treat this as a checklist derived from the code and from load tests on a
single machine, not as a record of a running deployment.

## Contents

1. [What you are deploying](#what-you-are-deploying)
2. [Environment and secrets](#environment-and-secrets)
3. [Database and migrations](#database-and-migrations)
4. [Running the app](#running-the-app)
5. [The maintenance loop](#the-maintenance-loop)
6. [Connection pool sizing](#connection-pool-sizing)
7. [Edge rate limits](#edge-rate-limits)
8. [Caching](#caching)
9. [Before enabling real payments](#before-enabling-real-payments)
10. [Scaling ladder](#scaling-ladder)
11. [Smoke test](#smoke-test)

## What you are deploying

- **One ASGI app** (`marketplace.api:app`) serving the JSON API under `/v1`,
  the server-rendered pages, `/static`, `/docs` and `/healthz`.
- **An in-process maintenance loop**, started by the app's lifespan, that
  sends queued notifications, expires offers and stale payments, retries
  failed webhooks, and deletes expired rows. It must run on exactly one
  process (see [The maintenance loop](#the-maintenance-loop)).
- **Postgres** (16 is what CI and the compose file use). SQLite is for local
  work and tests only: on SQLite the app creates tables itself at startup,
  and row locks are no-ops.
- **Optional:** an SMTP relay for mail, and Stripe for real payments. With
  neither, mail is logged and payments go through an in-memory fake.

## Environment and secrets

Every setting comes from environment variables (a `.env` file is also read
if present). `.env.example` lists them all with comments.

**Secrets** (keep in your platform's secret store, never in the repo or a
committed `.env`):

| Variable | Notes |
|---|---|
| `DATABASE_URL` | contains the database password |
| `ADMIN_PASSWORD` | seeds the admin account; changing it and restarting rotates the credential and revokes admin sessions |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | only when enabling real payments; both or neither |
| `SMTP_PASSWORD` | if your relay needs one |

**Required for any public deployment:**

| Variable | Set to | Why |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…` | the default is a local SQLite file |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | your operator account | unset means no admin account exists |
| `COOKIE_SECURE` | `true` | otherwise the page session cookie lacks `Secure` |
| `TRUSTED_HOSTS` | `'["your.domain"]'` | default `["*"]` accepts any `Host` header |
| `CORS_ORIGINS` | `'["https://your.frontend"]'` or leave `[]` | only needed if a browser app on another origin calls the API |
| `BASE_URL` | your frontend's origin | used in verification and reset links |
| `LOG_FORMAT` | `json` (the default) | |

`TRUSTED_HOSTS` and `CORS_ORIGINS` are JSON arrays, not comma-separated
strings; `TRUSTED_HOSTS=api.example.com` fails at startup.

**Must stay unset in any deployed environment:** `ALLOW_UNSIGNED_WEBHOOKS`.
It opens the fake provider's unsigned webhook, which accepts forged payment
events. Startup refuses it alongside a Stripe key, but nothing stops it in
fake mode.

The app refuses to start if `STRIPE_SECRET_KEY` is set without
`STRIPE_WEBHOOK_SECRET`, or together with `ALLOW_UNSIGNED_WEBHOOKS`.

## Database and migrations

- Apply migrations before starting new code:
  `DATABASE_URL=<url> uv run alembic upgrade head`. The app does not run
  migrations itself on Postgres.
- The chain runs from an empty database on both SQLite and Postgres, and
  down to base and back (CI and `tests/test_migrations.py` check this).
  There is always a single head; `uv run alembic heads` shows it.
- If you use an external pooler in transaction mode (see [Connection pool sizing](#connection-pool-sizing)), run
  migrations over a **direct** connection, not through the pooler: DDL and
  session-level settings don't belong in transaction pooling.
- The admin account is created or rotated at startup by every instance.
  Two instances starting at the same moment against a database with no
  admin can collide on the unique `(email, role)` constraint; the loser
  fails to start and succeeds on restart. Start one instance first, or let
  your platform restart the other.

## Running the app

```bash
uvicorn marketplace.api:app --host 0.0.0.0 --port "$PORT"
```

- **TLS** is expected at the edge (load balancer or reverse proxy). Set
  `COOKIE_SECURE=true` once it is.
- **Behind a proxy**, pass uvicorn's `--proxy-headers` and set
  `--forwarded-allow-ips` to your proxy's addresses, so the scheme and client
  address it reports are trusted only from the proxy.
- **Health check:** `GET /healthz` returns `{"status": "ok"}`. It does not
  touch the database, so it tells you the process is up, not that Postgres
  is reachable.
- **Workers.** Each uvicorn worker (`--workers N`) is a separate process with
  its own threadpool, its own connection pool and its own maintenance loop
  (unless disabled). Count workers, not containers, when sizing (see the maintenance loop and pool sizing
  sections).
- **Security headers** (`Content-Security-Policy`, `X-Frame-Options` or
  `frame-ancestors`, `Strict-Transport-Security`, `X-Content-Type-Options`)
  are not set by the app. Add them at the edge before serving the pages
  publicly.
- **Logs** are JSON lines on stdout with a `request_id`; the response's
  `X-Request-ID` header carries the same id. Access logs never contain
  headers, bodies or query strings.

## The maintenance loop

The loop runs wherever `MAINTENANCE_ENABLED` is true (the default). Every
`NOTIFY_DRAIN_SECONDS` (5) it sends pending notifications; every
`SWEEP_INTERVAL_SECONDS` (60) it expires offers (re-matching dispatch jobs),
releases payments that never settled, deletes expired quotes, sessions,
email tokens and old retention rows, and retries due webhook events.

- **Run it on exactly one process.** Set `MAINTENANCE_ENABLED=false` on
  every other instance and worker. Its jobs are safe to run concurrently
  (offer expiry and webhook retry claim rows with `SKIP LOCKED`), but N
  runners do N times the work. With `--workers N` on the maintenance
  instance, every worker starts a loop; run that instance with one worker
  or accept the duplicated work.
- **Never scale the maintenance instance to zero.** Correctness-critical
  expiry also runs lazily on reads, but without the loop no notification is
  sent, no failed webhook is retried and nothing is cleaned up.
- `POST /v1/admin/jobs/sweep` runs the sweep and housekeeping once on
  demand, and `POST /v1/admin/notifications/drain` sends pending mail.

## Connection pool sizing

Each process has a threadpool (Starlette's default, 40 tokens) that runs the
synchronous handlers, and a SQLAlchemy connection pool. If the pool is
smaller than the threadpool, requests holding threads wait for connections
while requests holding connections wait for threads to finish; only the
checkout timeout breaks that. The defaults are sized so this can't happen:

| Setting | Default | Meaning |
|---|---|---|
| `DB_POOL` | `queue` | `null` disables app-side pooling (see below) |
| `DB_POOL_SIZE` | 10 | connections kept open |
| `DB_MAX_OVERFLOW` | 30 | extra connections opened under load |
| `DB_POOL_TIMEOUT_SECONDS` | 5 | wait for a connection before answering 503 |

10 + 30 = 40, one connection per threadpool token. A request that can't get
a connection in 5 seconds gets a 503 with `Retry-After: 1` rather than
stalling, and the 503 is never stored for idempotent replay. Occasional 503s
under a burst are load shedding working as intended; sustained 503s mean the
budget below is too small.

**Fit the total under Postgres `max_connections`:**

```
processes × (DB_POOL_SIZE + DB_MAX_OVERFLOW)  +  headroom  ≤  max_connections
processes = replicas × uvicorn workers per replica
```

Headroom covers migrations, an admin `psql`, monitoring and the maintenance
loop's own sessions. For example, 2 replicas × 2 workers × 40 = 160
connections, which already exceeds a Postgres configured with the common
default of 100. Either lower `DB_MAX_OVERFLOW` per process (accepting more
503s at peak), raise `max_connections`, or use an external pooler.

**The threadpool size is not a setting.** It is the anyio default for the
process. Raising it means a code change, and the pool has to be raised with
it; otherwise the deadlock described above returns.

**External pooler (`DB_POOL=null`).** With a pooler such as PgBouncer in
front of Postgres, set `DB_POOL=null` so each checkout opens a connection to
the pooler and the pooler owns pooling; `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` and
`DB_POOL_TIMEOUT_SECONDS` are then unused. Caveats:

- The app uses `SELECT … FOR UPDATE` and `SKIP LOCKED` inside transactions,
  which works in transaction pooling mode because each lock lives within one
  transaction. Run migrations over a direct connection (see [Database and migrations](#database-and-migrations)).
- The `psycopg` driver uses server-side prepared statements for repeated
  queries. Check that your pooler supports prepared statements in
  transaction mode, or disable them on the connection. This repository has
  not been tested behind an external pooler.
- Without the app-side pool there is no 5-second checkout timeout; overload
  shows up as the pooler's own queueing or errors instead of a 503.

**Locks held across provider calls.** Accept, complete and cancel hold row
locks and a pooled connection while calling the payment provider. The fake
provider answers instantly; a real one adds its network latency to how long
each of those requests occupies a connection. Budget for that before
switching to real payments.

## Edge rate limits

The app has no rate limiting. Signup, login and password-reset confirm hash
or verify passwords with argon2, which costs real CPU and memory per
attempt, so they are the cheapest way to load the server and the obvious
brute-force target. Password-reset request sends mail on every call for an
existing account. Put per-client-IP limits in front of at least:

| Route | Why |
|---|---|
| `POST /v1/auth/login` | argon2 verify; password guessing |
| `POST /v1/auth/signup` | argon2 hash; account creation |
| `POST /v1/auth/password-reset/confirm` | argon2 hash |
| `POST /v1/auth/password-reset/request` | sends mail; mail bombing |
| `POST /login` | page login, same code path as the JSON login |
| `POST /signup` | page signup, same code path as the JSON signup |

These are human-paced actions; a few attempts per minute per IP is
generous. Also worth limiting: `POST /v1/quotes` and `POST /v1/jobs`
(write-heavy and lock-taking), and `POST /v1/auth/verify`. App-level
idempotency keys and row locks remain the correctness backstop; edge limits
are load protection.

Do **not** rate-limit `POST /v1/payments/webhook` by client IP in a way that
can drop provider deliveries; the provider retries, but dropped events delay
settlement. If you restrict it, restrict by the provider's published source
addresses.

## Caching

The app sends no `Cache-Control` headers, so caching is entirely an edge
decision.

**Safe to cache:**

- `/static/*` (stylesheet and the vendored HTMX file). They change only on
  deploy; cache them and purge on release.

**Cacheable with care:**

- `GET /v1/catalog/*` is unauthenticated and identical for every caller,
  but `price_from` is computed live from the buyer pricing pipeline, which
  can include demand-based surge. A short TTL (seconds) trades freshness for
  load; the price on a real quote is computed fresh regardless.

**Never cache:**

- The HTML pages (`/`, `/listings/*`, `/sellers/*`) for any request that
  carries a `session_token` cookie: the header shows the signed-in user's
  display name. Caching them at all is only safe if the cache key or a
  bypass rule excludes cookie-bearing requests.
- Anything with an `Authorization` header or a `Set-Cookie` response.
- `/v1/auth/*`, `/signup`, `/login`, `/logout`.
- Every buyer, seller and admin route under `/v1`, and
  `POST /v1/payments/webhook`.
- `/healthz` (a cached health check hides a dead process).

## Before enabling real payments

Work through this before setting a live `STRIPE_SECRET_KEY`:

- [ ] **Webhook secret.** Create the webhook endpoint in the provider
  dashboard pointing at `POST /v1/payments/webhook` and set
  `STRIPE_WEBHOOK_SECRET`. The app refuses to start without it.
- [ ] **`ALLOW_UNSIGNED_WEBHOOKS` is unset** everywhere. After deploying,
  check that an unsigned POST to the webhook answers 400 (invalid
  signature), not 200.
- [ ] **`COOKIE_SECURE=true`** and TLS terminated in front of the app.
- [ ] **`TRUSTED_HOSTS`** narrowed to your hostnames.
- [ ] **`CORS_ORIGINS`** set to your frontend origins, or left empty if
  nothing cross-origin calls the API. The app enables credentials on CORS
  when origins are set, so never list origins you don't control.
- [ ] **Edge rate limits** on the argon2 routes are live.
- [ ] **Security headers** added at the edge.
- [ ] **Exactly one maintenance loop** is running (it retries failed
  webhooks and sends dead-letter alerts).
- [ ] **An admin email that is read.** Dead-lettered webhooks, failed
  payouts, disputes and chargebacks notify every admin, and the money ones
  can't be muted. Configure `SMTP_HOST` so they arrive.
- [ ] **Fee config** (`PUT /v1/admin/config/fees`) matches your provider
  pricing, and the margin floor is set; the floor is enforced net of that
  estimate.
- [ ] **Test mode first.** Run the full escrow loop against the provider's
  test mode: onboarding, a charge that settles by webhook, a decline, a
  cancel, a completion and payout, a partial dispute refund. The adapter's
  automated tests cover signature verification and event mapping only.
- [ ] **Pool budget** accounts for provider latency (see [Connection pool sizing](#connection-pool-sizing)).

## Scaling ladder

In order; each step assumes the previous ones are done.

0. **Get the concurrency budget right (configuration).** Pool, threadpool
   and Postgres `max_connections` sized against each other (see [Connection pool sizing](#connection-pool-sizing)).
   This is the step that decides whether overload degrades into 503s or
   into timeouts.
1. **Edge rate limits and caching** (see [Edge rate limits](#edge-rate-limits) and [Caching](#caching)). Limits protect the
   argon2 routes; caching helps static assets and, with a short TTL, the
   public catalog. Neither helps authenticated or booking traffic.
2. **A bigger instance.** More CPU mostly helps argon2 throughput, which is
   CPU-bound. It does not raise the per-process pool or threadpool limits,
   so on its own it does not move the point where requests queue.
3. **More workers or replicas**, with `MAINTENANCE_ENABLED=true` on exactly
   one. Re-check the connection formula every time the process count
   changes; this is where an external pooler starts to pay off.
4. **A larger database tier** (connection ceiling, CPU, storage). Only
   binding once the steps above have raised the app-side connection count.

The load-test harness in [`loadtest/`](../loadtest/README.md) compares two
builds or configurations under identical conditions. Its numbers come from
one machine running Locust, the app and Postgres together, with the fake
provider answering instantly; use them for before-and-after comparisons, not
as production capacity.

## Smoke test

```bash
curl https://<host>/healthz                         # {"status":"ok"}
curl https://<host>/v1/catalog/listings | head      # JSON list (empty until sellers list)
curl -I https://<host>/                             # 200, HTML
curl -X POST https://<host>/v1/payments/webhook     # 404 in fake mode, 400 with Stripe
```

Then sign in as the admin through `POST /v1/auth/login` and check
`GET /v1/admin/stats`: `notifications.pending` should drain toward zero if
the maintenance loop is running, and `GET /v1/admin/webhooks?status=failed`
should be empty.
