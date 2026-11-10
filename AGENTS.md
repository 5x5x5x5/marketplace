# AGENTS.md

Instructions for coding agents working in this repository. **`CLAUDE.md` is
the source of truth** for the invariants ("Non-negotiables") and the subtle
bits; read it before changing code. This file only orients you and points
at the workflow; it does not repeat the rules.

## What this is

A two-sided marketplace template: FastAPI, Pydantic v2, SQLAlchemy 2.0 +
Alembic, Postgres in production, SQLite for local work and tests. The buyer's
price and the seller's payout are computed independently by pricing
pipelines and the platform keeps the spread, so **which side sees which
number** is the core correctness property, enforced by separate response
models per role.

## Commands

```bash
uv sync                                          # install
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
.claude/skills/gate/gate.sh [--pg]               # the same gate + demo; --pg adds Postgres + migration round trip
uv run python scripts/demo.py                    # full lifecycle, headless
uv run python scripts/seed.py                    # seed data, print bearer tokens for /docs
uv run uvicorn marketplace.api:app --reload      # API on SQLite by default
docker compose up -d db && uv run alembic upgrade head   # Postgres
```

Never pipe gate commands (the pipe's exit status hides failures). Pyright
runs in strict mode over `src/` and `tests/`. Run the suite on Postgres too
for anything touching migrations, locking, payments, or webhooks; some tests
only run there.

## Layout

```
src/marketplace/
  api.py            app assembly only: middleware, lifespan, router mounting, error mapping
  routers/          thin JSON routers (buyer, seller, admin, catalog, payments, reports, preferences)
  services/         domain logic: booking, jobs, offers, webhooks, disputes, payouts, maintenance, ...
  errors.py         domain errors that services raise (mapped to HTTP once, in api.py)
  auth.py           sessions, signup/login, principal resolution (bearer header or cookie)
  pages.py          server-rendered pages (call services; never import api.py or routers/)
  catalog.py        the one implementation of catalog visibility and "from" prices
  models.py         Pydantic views, one family per role: Buyer*, Seller*, Admin*, Catalog*
  entities.py       ORM (never returned from a route unmapped)
  pricing.py        adjusters (pure, @register)       matching.py  strategies (pure, @register_strategy)
  config.py         snapshots the pure core runs on     repo.py      loads them from the DB
  db.py             sessions and CommitRoute (commit before the response is sent)
  payments/         port.py + fake.py + stripe_provider.py (the only place stripe is imported)
  notifications.py  transactional outbox            idempotency.py  Idempotency-Key replay
src/marketplace_mcp/  ops MCP server: the admin HTTP API as operator tools (never imports the app)
migrations/versions/  Alembic, must run on SQLite and Postgres, no app imports
tests/                pytest; conftest.py points at a temp SQLite DB unless DATABASE_URL is set
docs/superpowers/     design specs and implementation plans for every phase
docs/tryout-findings-*.md   findings from driving a live server
```

## Workflow

Features are built as phases: design spec -> implementation plan with small
tasks -> each task test-first with the gate green -> narrow adversarial
review of the whole branch -> fix wave -> live try-out -> docs -> merge.
Specs and plans live in `docs/superpowers/specs/` and `docs/superpowers/plans/`.

Agents with Claude Code get this as executable configuration under
`.claude/` (other agents can read the same files as instructions):

| Need | Read |
|---|---|
| Run the gate | `.claude/skills/gate/SKILL.md` |
| Plan or build a phase | `.claude/skills/feature-phase/SKILL.md` |
| Touch money or job/payment status | `.claude/skills/money-path/SKILL.md` |
| Add an endpoint, page, or notification | `.claude/skills/endpoint-or-page/SKILL.md` |
| Add a migration | `.claude/skills/migration/SKILL.md` |
| Add a pricing adjuster or matching strategy | `.claude/skills/pricing-extension/SKILL.md` |
| Try it against a running server | `.claude/skills/tryout/SKILL.md` |
| Review a diff | `.claude/agents/*-reviewer.md` |
| Background agents (issue to PR, PR review, weekly upkeep) | `docs/background-agents.md` |
| Operate the marketplace from an agent | `src/marketplace_mcp/README.md` |

`docs/agent-setup.md` explains why each piece exists.

## Hard rules worth repeating here

These are the ones a tool can't always catch for you; the full list is in
`CLAUDE.md`.

- Identity comes from the authenticated principal, never from a request body.
- Never return a hand-built dict or an ORM row from a role-scoped route; use
  that role's view model.
- Money is `Decimal`, quantized with `models.to_money`.
- Don't skip, weaken, or delete a test to get green.
