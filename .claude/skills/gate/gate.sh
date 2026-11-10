#!/usr/bin/env bash
# The repo gate. Stops at the first failing step and says which one.
#
#   .claude/skills/gate/gate.sh          static checks + SQLite suite + demo
#   .claude/skills/gate/gate.sh --pg     ...then the Postgres stage too
#   .claude/skills/gate/gate.sh --pg-only
#
# Postgres stage, against a scratch database it creates and drops:
#   alembic upgrade head -> downgrade base -> upgrade head   (migration round trip)
#   alembic check                                            (models == migrations)
#   pytest on the migrated schema                            (incl. the PG-only race tests)
#
# Env: PG_ADMIN_URL (default postgresql://marketplace:marketplace@localhost:5432/postgres,
# the docker-compose credentials), GATE_KEEP_DB=1 to keep the scratch database.
#
# Exit codes are the steps' own; nothing is piped, so a failure can't be masked.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

run_sqlite=1
run_pg=0
for arg in "$@"; do
  case "$arg" in
    --pg) run_pg=1 ;;
    --pg-only) run_pg=1; run_sqlite=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "gate.sh: unknown argument $arg" >&2; exit 64 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
done_steps=()

# The app reads DATABASE_URL from the environment and from .env; an exported
# Postgres URL from an earlier command must not silently turn the "SQLite"
# stage into a second Postgres run, and a .env must not leak in either.
unset DATABASE_URL

if [ "$run_sqlite" = 1 ]; then
  step "ruff check";            uv run ruff check .
  step "ruff format --check";   uv run ruff format --check .
  step "pyright (strict)";      uv run pyright
  step "pytest (SQLite)";       uv run pytest -q
  step "demo (full lifecycle)"; uv run python scripts/demo.py >/dev/null
  done_steps+=("static" "sqlite" "demo")
fi

if [ "$run_pg" = 1 ]; then
  admin_url="${PG_ADMIN_URL:-postgresql://marketplace:marketplace@localhost:5432/postgres}"
  pg() { uv run python - "$admin_url" "$@" <<'PY'
import sys
import psycopg
url, action, *rest = sys.argv[1:]
try:
    with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
        if action == "create":
            conn.execute(f'CREATE DATABASE "{rest[0]}"')
        elif action == "drop":
            conn.execute(f'DROP DATABASE IF EXISTS "{rest[0]}" WITH (FORCE)')
except psycopg.OperationalError as exc:
    print(f"postgres unreachable: {exc}".strip(), file=sys.stderr)
    sys.exit(3)
PY
  }

  step "postgres reachable"
  if ! pg ping; then
    if command -v docker >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
      echo "starting the compose db service (never 'down -v': the volume may be shared)"
      docker compose up -d --wait db
      pg ping
    else
      echo "gate.sh: no Postgres and no docker; report the Postgres gate as NOT RUN." >&2
      exit 3
    fi
  fi

  db="gate_$(date +%s)_$$"
  base="${admin_url%/*}"
  export DATABASE_URL="${base/postgresql:/postgresql+psycopg:}/$db"
  pg create "$db"
  if [ "${GATE_KEEP_DB:-0}" != 1 ]; then
    trap 'pg drop "$db" || true' EXIT
  fi
  echo "scratch database: $db"

  step "alembic upgrade head (empty Postgres)"; uv run alembic upgrade head
  step "alembic downgrade base";                uv run alembic downgrade base
  step "alembic upgrade head (again)";          uv run alembic upgrade head
  step "alembic heads (exactly one)"
  heads="$(uv run alembic heads 2>/dev/null)"
  echo "$heads"
  [ "$(printf '%s\n' "$heads" | grep -c .)" = 1 ] || { echo "gate.sh: multiple heads" >&2; exit 1; }
  step "alembic check (models match migrations)"; uv run alembic check
  step "pytest (Postgres, migrated schema)";      uv run pytest -q
  done_steps+=("postgres")
fi

printf '\nGATE PASSED: %s\n' "${done_steps[*]}"
