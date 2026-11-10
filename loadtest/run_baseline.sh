#!/usr/bin/env bash
# Capacity baseline: fresh Postgres database -> migrate -> seed -> uvicorn
# (single process by default, no reload) -> staged headless Locust runs ->
# per-stage CSVs plus summary.json / summary.md in loadtest/results/<stamp>/.
# See loadtest/README.md for interpretation.
#
#   bash loadtest/run_baseline.sh
#   BROWSE_USERS="10 50" LIFECYCLE_USERS="" LOGIN_USERS="" STAGE_SECONDS=10 \
#       bash loadtest/run_baseline.sh                      # quick smoke
#
# Stage matrix (space-separated user counts; empty skips that scenario):
#   BROWSE_USERS="10 25 50"  LIFECYCLE_USERS="6 12 24"  LOGIN_USERS="5 10"
#   STAGE_SECONDS=60  SPAWN_RATE=<users/s, default = all users at once>
#   SCALE_CATALOG_SIZE=<n>  SCALE_USERS=25   # optional final browse stage on a bigger catalog
# Data:    SELLERS=50  BUYERS=10  CATALOG_SIZE=100  SELLER_CAPACITY=3
# Server:  WORKERS=1  PORT=8123  (pool / threadpool overrides in the environment
#          are passed through to the app and recorded in conditions.json)
# Database: DB_NAME=marketplace_load  DB_ADMIN_URL=postgresql://marketplace:marketplace@localhost:5432/postgres
#          RESET_DB=1 drops and recreates DB_NAME first (the name must contain "load"
#          unless ALLOW_ANY_DB=1). RESET_DB=0 reuses it (seeding is idempotent).
# Output:  RESULTS_ROOT=loadtest/results
#
# Needs: a reachable Postgres, psql, uv, and `uv sync --group loadtest`.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8123}"
WORKERS="${WORKERS:-1}"
SELLERS="${SELLERS:-50}"
BUYERS="${BUYERS:-10}"
CATALOG_SIZE="${CATALOG_SIZE:-100}"
SELLER_CAPACITY="${SELLER_CAPACITY:-3}"
BROWSE_USERS="${BROWSE_USERS-10 25 50}"
LIFECYCLE_USERS="${LIFECYCLE_USERS-6 12 24}"
LOGIN_USERS="${LOGIN_USERS-5 10}"
STAGE_SECONDS="${STAGE_SECONDS:-60}"
SCALE_CATALOG_SIZE="${SCALE_CATALOG_SIZE:-}"
SCALE_USERS="${SCALE_USERS:-25}"
DB_NAME="${DB_NAME:-marketplace_load}"
DB_ADMIN_URL="${DB_ADMIN_URL:-postgresql://marketplace:marketplace@localhost:5432/postgres}"
RESET_DB="${RESET_DB:-1}"
RESULTS_ROOT="${RESULTS_ROOT:-loadtest/results}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RESULTS="$RESULTS_ROOT/$STAMP"
HOST="http://127.0.0.1:$PORT"
TOKENS="loadtest/.tokens.json"

# Same server, credentials and host as the admin URL, different database.
DB_URL_PLAIN="${DB_ADMIN_URL%/*}/$DB_NAME"
export DATABASE_URL="postgresql+psycopg://${DB_URL_PLAIN#postgresql://}"
export ADMIN_EMAIL="admin@loadtest.dev"
export ADMIN_PASSWORD="load-test-admin-password"
# Pin provider/mail env: the deterministic fake payment provider and console
# mail adapter only; a developer .env must never leak through. Fake-provider
# webhooks are unsigned, so the app must be told to accept them.
export STRIPE_SECRET_KEY="" STRIPE_WEBHOOK_SECRET="" SMTP_HOST=""
export ALLOW_UNSIGNED_WEBHOOKS=true
export LOADTEST_TOKENS="$PWD/$TOKENS"

if [ "$RESET_DB" = "1" ] && [ "${ALLOW_ANY_DB:-0}" != "1" ] && [[ "$DB_NAME" != *load* ]]; then
    echo "refusing to drop '$DB_NAME': name must contain 'load' (or set ALLOW_ANY_DB=1)" >&2
    exit 2
fi

mkdir -p "$RESULTS"
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill -- "-$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "== Database $DB_NAME =="
if [ "$RESET_DB" = "1" ]; then
    psql "$DB_ADMIN_URL" -v ON_ERROR_STOP=1 -q \
        -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)" \
        -c "CREATE DATABASE \"$DB_NAME\""
fi
uv run alembic upgrade head

echo "== Seed (sellers=$SELLERS buyers=$BUYERS catalog>=$CATALOG_SIZE) =="
uv run python loadtest/seed_loadtest.py --sellers "$SELLERS" --buyers "$BUYERS" \
    --catalog-size "$CATALOG_SIZE" --capacity "$SELLER_CAPACITY" --tokens "$TOKENS"

echo "== Server (workers=$WORKERS) =="
setsid uv run uvicorn marketplace.api:app --host 127.0.0.1 --port "$PORT" \
    --workers "$WORKERS" --log-level warning >"$RESULTS/uvicorn.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 120); do
    curl -sf "$HOST/healthz" >/dev/null && break
    sleep 0.5
done
curl -sf "$HOST/healthz" >/dev/null || { echo "server failed to start; see $RESULTS/uvicorn.log"; exit 1; }

catalog_size() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["catalog_size"])' "$TOKENS"
}

# Record the conditions next to the numbers: a before/after comparison is
# meaningless without them.
RESULTS_DIR="$RESULTS" python3 - <<PY
import json, os, platform, subprocess, sys

def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None

mem_kb = None
try:
    mem_kb = int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1])
except Exception:
    pass
env_keys = sorted(k for k in os.environ if any(t in k.upper() for t in ("POOL", "ANYIO", "UVICORN", "WEB_CONCURRENCY", "THREADS")))
conditions = {
    "timestamp": "$STAMP",
    "git_commit": sh("git", "rev-parse", "--short", "HEAD"),
    "git_branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD"),
    "git_dirty": bool(sh("git", "status", "--porcelain", "--untracked-files=no")),
    "workers": int("$WORKERS"),
    "env_overrides": {k: os.environ[k] for k in env_keys},
    "catalog_size": int("$(catalog_size)"),
    "sellers": int("$SELLERS"),
    "buyers": int("$BUYERS"),
    "seller_capacity": int("$SELLER_CAPACITY"),
    "cpu_cores": os.cpu_count(),
    "mem_gb": round(mem_kb / 1048576, 1) if mem_kb else None,
    "loadgen_shared_box": True,
    "payment_provider": "fake",
    "postgres": sh("psql", "$DB_ADMIN_URL", "-Atc", "SHOW server_version"),
    "python": platform.python_version(),
    "locust": sh("uv", "run", "locust", "--version"),
    "stage_seconds": int("$STAGE_SECONDS"),
    "stage_matrix": {
        "browse": "$BROWSE_USERS".split(),
        "lifecycle": "$LIFECYCLE_USERS".split(),
        "login": "$LOGIN_USERS".split(),
    },
}
json.dump(conditions, open(os.path.join(os.environ["RESULTS_DIR"], "conditions.json"), "w"), indent=2)
PY

jobs_by_status() { # cumulative jobs per status as a JSON object
    psql "$DB_URL_PLAIN" -At -F ' ' -c "SELECT status::text, count(*) FROM jobs GROUP BY 1" |
        python3 -c 'import json,sys; print(json.dumps({p[0]: int(p[1]) for p in (l.split() for l in sys.stdin) if len(p) == 2}))'
}

stage() { # stage <label> <users> <locust user classes...>
    local label="$1" users="$2" rc=0
    shift 2
    local load_before
    load_before="$(cut -d' ' -f1 /proc/loadavg)"
    echo "== stage: $label ($users users, ${STAGE_SECONDS}s, load avg $load_before) =="
    uv run locust -f loadtest/locustfile.py --headless --host "$HOST" \
        -u "$users" -r "${SPAWN_RATE:-$users}" -t "${STAGE_SECONDS}s" --only-summary \
        --csv "$RESULTS/$label" "$@" 2>"$RESULTS/$label.log" || rc=$?
    [ "$rc" -eq 0 ] || echo "  (locust exit $rc; non-zero is normal when any request failed, see $label.log)"
    python3 - "$label" "$users" "$rc" "$load_before" "$(cut -d' ' -f1 /proc/loadavg)" \
        "$(jobs_by_status)" "$(catalog_size)" >>"$RESULTS/stages.jsonl" <<'PY'
import json, os, sys
label, users, rc, before, after, jobs, catalog = sys.argv[1:8]
print(json.dumps({
    "label": label,
    "scenario": label.split("-")[0],
    "users": int(users),
    "seconds": int(os.environ["STAGE_SECONDS"]),
    "locust_exit": int(rc),
    "loadavg_before": float(before),
    "loadavg_after": float(after),
    "catalog_size": int(catalog),
    "jobs_total": json.loads(jobs),
}))
PY
    sleep 2 # let in-flight work drain before the next stage's clock starts
}

for u in $BROWSE_USERS; do stage "browse-$u" "$u" BrowseUser; done
for u in $LIFECYCLE_USERS; do stage "lifecycle-$u" "$u" LifecycleBuyer LifecycleSeller; done
for u in $LOGIN_USERS; do stage "login-$u" "$u" LoginUser; done

if [ -n "$SCALE_CATALOG_SIZE" ]; then
    echo "== scale catalog to >= $SCALE_CATALOG_SIZE listings =="
    uv run python loadtest/seed_loadtest.py --sellers "$SELLERS" --buyers "$BUYERS" \
        --catalog-size "$SCALE_CATALOG_SIZE" --capacity "$SELLER_CAPACITY" --tokens "$TOKENS" >/dev/null
    stage "browse-scale-$SCALE_USERS" "$SCALE_USERS" BrowseUser
fi

python3 loadtest/summarize.py "$RESULTS"
echo "== done: $RESULTS =="
