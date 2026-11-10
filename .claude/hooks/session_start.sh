#!/usr/bin/env bash
# SessionStart (startup|resume): make the gate runnable immediately in a cloud
# session, where the container starts without .venv.
#
# Only acts when CLAUDE_CODE_REMOTE=true; a local session keeps whatever venv
# the developer manages. Plain stdout becomes context for Claude, so the last
# lines say what is and isn't available (Postgres is often absent in cloud
# containers; the gate skill then reports the Postgres run as not done rather
# than skipping it silently).
#
# Fails open: every branch exits 0. A failed sync is reported, never fatal.

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
[ -f pyproject.toml ] && [ -f uv.lock ] || exit 0

if ! command -v uv >/dev/null 2>&1; then
  echo "session-start: uv is not installed; install it before running the gate."
  exit 0
fi

log="$(mktemp 2>/dev/null || echo /tmp/session-start-uv.log)"
if uv sync --frozen --quiet >"$log" 2>&1; then
  echo "session-start: dependencies synced from uv.lock (uv sync --frozen)."
else
  echo "session-start: uv sync --frozen failed; first lines:"
  head -n 5 "$log"
fi

if command -v pg_isready >/dev/null 2>&1 && pg_isready -h localhost -p 5432 -q 2>/dev/null; then
  echo "session-start: Postgres is reachable on localhost:5432 (gate.sh --pg will work)."
else
  echo "session-start: no Postgres on localhost:5432; run the SQLite gate and report the Postgres gate as not run."
fi
exit 0
