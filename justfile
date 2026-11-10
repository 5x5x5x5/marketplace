# Common commands. `just gate` is exactly the canonical gate in CLAUDE.md.

default:
    @just --list

# The full gate: lint, format check, types, tests.
gate:
    uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest

# Test suite on SQLite (default).
test *args:
    uv run pytest {{args}}

# Test suite on Postgres. Override the URL, e.g. DATABASE_URL=... just test-pg
test-pg *args:
    DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace}" uv run pytest {{args}}

# Apply migrations to the configured database.
migrate:
    uv run alembic upgrade head

# Fast: the executable CLAUDE.md invariants only.
arch:
    uv run pytest tests/test_architecture.py -q

# Security checks CI runs (needs network for the registry rulesets).
semgrep:
    uvx semgrep==1.184.0 --test --config .semgrep/rules .semgrep/tests --metrics=off
    uvx semgrep==1.184.0 --config .semgrep/rules --config p/python --config p/fastapi --exclude-rule python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure --error --metrics=off src scripts

audit:
    uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/marketplace-requirements.txt
    uvx pip-audit==2.10.1 --no-deps --disable-pip -r /tmp/marketplace-requirements.txt

demo:
    uv run python scripts/demo.py

seed:
    uv run python scripts/seed.py

serve:
    uv run uvicorn marketplace.api:app --reload
