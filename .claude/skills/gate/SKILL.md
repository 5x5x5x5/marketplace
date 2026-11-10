---
name: gate
description: Run this repo's full verification gate (ruff, ruff format, strict pyright, pytest, demo) on SQLite and, with --pg, on a scratch Postgres database including the Alembic up/down/up round trip and a models-vs-migrations drift check. Use before every commit, before reporting a task done, after a migration or money-path change, and whenever someone asks "does it pass".
allowed-tools: Bash(.claude/skills/gate/gate.sh *)
---

# The gate

Run from the repo root:

```bash
.claude/skills/gate/gate.sh          # static + SQLite suite + demo   (~75 s)
.claude/skills/gate/gate.sh --pg     # ...plus the Postgres stage      (~70 s more)
```

The canonical command in `CLAUDE.md` is the same first stage written out:
`uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.
If a `justfile` exists, `just gate` may wrap the same steps; the script here
is the reference and does not depend on it.

## When `--pg` is required

Run the Postgres stage whenever the change touches a migration, an entity,
row locking, a status transition, webhooks, payments, or anything else on
the money path, and always before a phase merges. The SQLite suite cannot see:

- **Parallel-write races.** Eight tests (`skipif(not IS_POSTGRES)`) only run
  on Postgres, because SQLite serializes writers. The cancel-vs-complete
  double-pay (finding F5) and the ABBA lock-order deadlock between cancel and
  the webhook applier are this class.
- **Migrations against the real constraint names.** The suite builds its
  schema with `create_all`, so a migration that only works on one engine
  passed every test until `tests/test_migrations.py` was added (the listings
  migration shipped with an `add_column(..., ForeignKey)` SQLite cannot run).
  The script runs `upgrade head -> downgrade base -> upgrade head` on an empty
  Postgres DB, then `alembic check`, then the suite on that migrated schema,
  so a model change without a migration (or the reverse) fails here.

## Rules

- **Never pipe a gate command** (`uv run pytest | tail` exits with `tail`'s
  status). A green-looking tail over a red run is how a failure gets reported
  as a pass. The script never pipes; if you run steps by hand, run them bare.
- **Report counts, not adjectives:** "SQLite: N passed, 8 skipped; Postgres:
  M passed". The 8 SQLite skips are the Postgres-only tests; any other skip
  count is worth a sentence.
- **No Postgres available** (common in cloud sessions; the session-start
  hook says which): the script exits 3. Report "Postgres gate: NOT RUN (no
  database)". Never describe a SQLite-only run as the full gate.
- The script starts the compose `db` service if Postgres is down and docker
  exists. **Never `docker compose down -v`**: the volume can be shared with
  other checkouts; the settings deny it.
- A failure in a test you didn't touch is still yours to explain. Do not
  skip, weaken, or delete a test to get green; if a test pins behavior you are
  deliberately changing, change it and say so.
- Scratch databases are named `gate_<epoch>_<pid>` and dropped on exit;
  `GATE_KEEP_DB=1` keeps one for inspection. `PG_ADMIN_URL` points elsewhere.

## Not covered here

Real-socket behavior (commit-before-response, idempotency replay) is covered
by `tests/test_live_server.py` inside the suite, and against a running server
by the `tryout` skill. The hook self-tests run separately:
`uv run pytest .claude/hooks/test_hooks.py -q`.
