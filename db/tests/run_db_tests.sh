#!/usr/bin/env bash
#
# run_db_tests.sh — spin up a disposable PostgreSQL cluster and verify the
# SAMePOS licensing + monetization cores end-to-end:
#   * migrations (024_licensing.sql, 025_monetization.sql) apply to a DB with
#     a sales_order stub;
#   * standalone schemas (licensing.schema.sql, monetization.schema.sql) apply
#     to a bare DB;
#   * behavioural assertions (behavior.sql, behavior_monetization.sql) all pass;
#   * migrations and standalone schemas produce the SAME objects;
#   * the migrations are idempotent (apply twice cleanly);
#   * the rollbacks (025 then 024 .down.sql) remove every object.
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
MIG2="$REPO_ROOT/db/migrations/025_monetization.sql"
DOWN2="$REPO_ROOT/db/migrations/025_monetization.down.sql"
STD2="$REPO_ROOT/db/schema/monetization.schema.sql"
BEH2="$REPO_ROOT/db/tests/behavior_monetization.sql"
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

echo "== apply migrations to 'licmig' (with sales_order stub present) =="
createdb_do licmig
psql_do -d licmig -q -c "CREATE TABLE sales_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_day_id bigint, created_at timestamptz DEFAULT now());"
psql_do -d licmig -q -f "$MIG"
psql_do -d licmig -q -f "$MIG2"
echo "   migrations applied."

echo "== apply standalone schemas to 'licstd' (bare DB) =="
createdb_do licstd
psql_do -d licstd -q -f "$STD"
psql_do -d licstd -q -f "$STD2"
echo "   standalone applied."

echo "== monetization refuses to apply without the licensing core =="
createdb_do licnodep
nodep_out="$(psql_do -d licnodep -q -f "$MIG2" 2>&1 || true)"
if printf '%s' "$nodep_out" | grep -q "requires the licensing core first"; then
    echo "   correctly refused with the guard's own message."
else
    echo "   FAILED: expected the licensing-core guard to reject this. Got:"
    printf '%s\n' "$nodep_out" | head -5; fail=1
fi

echo "== idempotency: apply migrations twice to 'licidem' =="
createdb_do licidem
psql_do -d licidem -q -c "CREATE TABLE sales_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_day_id bigint);"
psql_do -d licidem -q -f "$MIG"
psql_do -d licidem -q -f "$MIG2"
psql_do -d licidem -q -f "$MIG"
psql_do -d licidem -q -f "$MIG2"
echo "   migrations are idempotent (applied twice, no error)."

echo "== behavioural assertions (licmig: the migrations) =="
if psql_do -d licmig -f "$BEH"; then echo "   licensing behaviour OK."; else echo "   licensing behaviour FAILED."; fail=1; fi
if psql_do -d licmig -f "$BEH2"; then echo "   monetization behaviour OK."; else echo "   monetization behaviour FAILED."; fail=1; fi

echo "== behavioural assertions (licstd: the standalone schemas) =="
# The standalone schemas must BEHAVE identically, not merely declare the same
# object names — so run the same suites against the standalone build.
psql_do -d licstd -q -c "CREATE TABLE IF NOT EXISTS sales_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_day_id bigint, created_at timestamptz DEFAULT now());"
psql_do -d licstd -q -c "SELECT licence_attach_guard();"
if psql_do -d licstd -f "$BEH" >/dev/null; then echo "   licensing behaviour OK."; else echo "   licensing behaviour FAILED (standalone)."; fail=1; fi
if psql_do -d licstd -f "$BEH2" >/dev/null; then echo "   monetization behaviour OK."; else echo "   monetization behaviour FAILED (standalone)."; fail=1; fi

echo "== migrations vs standalone: identical licensing + billing objects =="
# Names alone would not catch a drifted function body, a missing trigger or a
# different default/constraint, so compare full definitions.
LIKE_REL="(c.relname LIKE 'licence%' OR c.relname LIKE 'billing%')"
OBJ_SQL="SELECT 'table:'||table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND (table_name LIKE 'licence%' OR table_name LIKE 'billing%') \
UNION ALL SELECT 'view:'||table_name FROM information_schema.views WHERE table_schema='public' AND (table_name LIKE 'licence%' OR table_name LIKE 'billing%') \
UNION ALL SELECT 'funcdef:'||pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND (p.proname LIKE 'licence%' OR p.proname LIKE 'billing%') \
UNION ALL SELECT 'column:'||c.relname||'.'||a.attname||':'||format_type(a.atttypid,a.atttypmod)||':'||coalesce(pg_get_expr(d.adbin,d.adrelid),'-')||':'||a.attnotnull \
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace \
  LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum \
  WHERE n.nspname='public' AND c.relkind IN ('r','v') AND a.attnum>0 AND NOT a.attisdropped AND $LIKE_REL \
UNION ALL SELECT 'constraint:'||c.relname||'.'||con.conname||':'||pg_get_constraintdef(con.oid) \
  FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace \
  WHERE n.nspname='public' AND $LIKE_REL \
UNION ALL SELECT 'index:'||pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND $LIKE_REL \
UNION ALL SELECT 'trigger:'||pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace \
  WHERE n.nspname='public' AND NOT t.tgisinternal AND (t.tgname LIKE 'trg_billing%' OR t.tgname LIKE 'trg_licence%') \
UNION ALL SELECT 'type:'||typname FROM pg_type WHERE typname='licence_mode' ORDER BY 1;"
objs_mig="$(psql_do -d licmig -Atc "$OBJ_SQL")"
objs_std="$(psql_do -d licstd -Atc "$OBJ_SQL")"
if [ "$objs_mig" = "$objs_std" ]; then
    echo "   equivalent ($(printf '%s\n' "$objs_mig" | grep -c . ) objects)."
else
    echo "   NOT equivalent:"; diff <(printf '%s\n' "$objs_mig") <(printf '%s\n' "$objs_std") || true; fail=1
fi

echo "== rollbacks remove every object (licmig; 025 then 024) =="
psql_do -d licmig -q -f "$DOWN2"
psql_do -d licmig -q -f "$DOWN"
remaining="$(psql_do -d licmig -Atc "$OBJ_SQL" | grep -c . || true)"
if [ "$remaining" = "0" ]; then echo "   rollback clean (0 objects remain)."; else
    echo "   rollback left $remaining object(s):"; psql_do -d licmig -Atc "$OBJ_SQL"; fail=1; fi

echo ""
if [ "$fail" -eq 0 ]; then echo "DB TESTS: ALL PASSED"; else echo "DB TESTS: FAILURES ABOVE"; fi
exit "$fail"
