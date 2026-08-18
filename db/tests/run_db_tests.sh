#!/usr/bin/env bash
#
# run_db_tests.sh — spin up a disposable PostgreSQL cluster and verify the
# SAMePOS licensing core end-to-end:
#   * migration (024_licensing.sql) applies to a DB with a sales_order stub;
#   * standalone schema (licensing.schema.sql) applies to a bare DB;
#   * behavioural assertions (behavior.sql) all pass;
#   * migration and standalone produce the SAME licensing objects;
#   * the migration is idempotent (applies twice cleanly);
#   * the rollback (024_licensing.down.sql) removes every licensing object.
#
# Never touches a real/venue database — it builds its own cluster in a temp dir
# and deletes it on exit. Requires PostgreSQL server binaries (initdb/pg_ctl).
#
set -euo pipefail

# --- Locate PostgreSQL server binaries ---------------------------------------
PGBIN=""
for c in "$(pg_config --bindir 2>/dev/null || true)" \
         /usr/lib/postgresql/*/bin /usr/pgsql-*/bin /opt/homebrew/opt/postgresql*/bin; do
    if [ -n "$c" ] && [ -x "$c/initdb" ] && [ -x "$c/pg_ctl" ]; then PGBIN="$c"; break; fi
done
if [ -z "$PGBIN" ]; then
    echo "SKIP: PostgreSQL server binaries (initdb/pg_ctl) not found." >&2
    exit 77   # conventional "skipped" code
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MIG="$REPO_ROOT/db/migrations/024_licensing.sql"
DOWN="$REPO_ROOT/db/migrations/024_licensing.down.sql"
STD="$REPO_ROOT/db/schema/licensing.schema.sql"
BEH="$REPO_ROOT/db/tests/behavior.sql"
PORT="${PGPORT_TEST:-55432}"

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/samepos-lic-test.XXXXXX")"

# Postgres refuses to run as root. If we are root, run the cluster as an
# unprivileged user (prefer an existing 'postgres' account).
RUN_AS=""
if [ "$(id -u)" -eq 0 ]; then
    if id postgres >/dev/null 2>&1; then RUN_AS="postgres"; else
        RUN_AS="pgtest_$$"; useradd -m "$RUN_AS"; fi
    chown -R "$RUN_AS":"$RUN_AS" "$TMPROOT" 2>/dev/null || chown -R "$RUN_AS" "$TMPROOT"
fi
as() { if [ -n "$RUN_AS" ]; then runuser -u "$RUN_AS" -- "$@"; else "$@"; fi; }

PGDATA="$TMPROOT/data"
LOG="$TMPROOT/pg.log"

cleanup() {
    # shellcheck disable=SC2317  # invoked indirectly via trap
    as "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
    # shellcheck disable=SC2317
    rm -rf "$TMPROOT" 2>/dev/null || true
    # shellcheck disable=SC2317
    if [ -n "${RUN_AS:-}" ] && [[ "$RUN_AS" == pgtest_* ]]; then
        userdel -r "$RUN_AS" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

psql_do() { as "$PGBIN/psql" -v ON_ERROR_STOP=1 -h "$TMPROOT" -p "$PORT" -U postgres "$@"; }
createdb_do() { as "$PGBIN/createdb" -h "$TMPROOT" -p "$PORT" -U postgres "$1"; }

echo "== initdb (throwaway cluster in $TMPROOT) =="
as "$PGBIN/initdb" -D "$PGDATA" -U postgres -A trust --no-locale -E UTF8 >/dev/null

echo "== start server (unix socket only, port $PORT) =="
as "$PGBIN/pg_ctl" -D "$PGDATA" -l "$LOG" -w -t 30 \
    -o "-c listen_addresses='' -k '$TMPROOT' -p $PORT" start

fail=0

echo "== apply migration to 'licmig' (with sales_order stub present) =="
createdb_do licmig
psql_do -d licmig -q -c "CREATE TABLE sales_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_day_id bigint, created_at timestamptz DEFAULT now());"
psql_do -d licmig -q -f "$MIG"
echo "   migration applied."

echo "== apply standalone schema to 'licstd' (bare DB) =="
createdb_do licstd
psql_do -d licstd -q -f "$STD"
echo "   standalone applied."

echo "== idempotency: apply migration twice to 'licidem' =="
createdb_do licidem
psql_do -d licidem -q -c "CREATE TABLE sales_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_day_id bigint);"
psql_do -d licidem -q -f "$MIG"
psql_do -d licidem -q -f "$MIG"
echo "   migration is idempotent (applied twice, no error)."

echo "== behavioural assertions (licmig) =="
if psql_do -d licmig -f "$BEH"; then echo "   behaviour OK."; else echo "   behaviour FAILED."; fail=1; fi

echo "== migration vs standalone: identical licensing objects =="
OBJ_SQL="SELECT 'table:'||table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name LIKE 'licence%' \
UNION ALL SELECT 'view:'||table_name FROM information_schema.views WHERE table_schema='public' AND table_name LIKE 'licence%' \
UNION ALL SELECT 'func:'||routine_name FROM information_schema.routines WHERE routine_schema='public' AND routine_name LIKE 'licence%' \
UNION ALL SELECT 'type:'||typname FROM pg_type WHERE typname='licence_mode' ORDER BY 1;"
objs_mig="$(psql_do -d licmig -Atc "$OBJ_SQL")"
objs_std="$(psql_do -d licstd -Atc "$OBJ_SQL")"
if [ "$objs_mig" = "$objs_std" ]; then
    echo "   equivalent ($(printf '%s\n' "$objs_mig" | grep -c . ) licensing objects)."
else
    echo "   NOT equivalent:"; diff <(printf '%s\n' "$objs_mig") <(printf '%s\n' "$objs_std") || true; fail=1
fi

echo "== rollback removes every licensing object (licmig) =="
psql_do -d licmig -q -f "$DOWN"
remaining="$(psql_do -d licmig -Atc "$OBJ_SQL" | grep -c . || true)"
if [ "$remaining" = "0" ]; then echo "   rollback clean (0 objects remain)."; else
    echo "   rollback left $remaining object(s):"; psql_do -d licmig -Atc "$OBJ_SQL"; fail=1; fi

echo ""
if [ "$fail" -eq 0 ]; then echo "DB TESTS: ALL PASSED"; else echo "DB TESTS: FAILURES ABOVE"; fi
exit "$fail"
