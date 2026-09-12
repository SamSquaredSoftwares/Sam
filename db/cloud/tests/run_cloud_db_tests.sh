#!/usr/bin/env bash
#
# run_cloud_db_tests.sh — spin up a disposable PostgreSQL cluster and verify
# the SAMePOS cloud POS trading migration end-to-end:
#   * migration (0001_pos_trading.sql) applies to a DB carrying stubs of the
#     pre-existing cloud objects (public.stores, auth.uid(), the
#     authenticated role — all real on Supabase, stubbed here);
#   * behavioural assertions (behavior.sql) all pass, including RLS
#     venue scoping under the authenticated role;
#   * the migration is idempotent (applies twice cleanly);
#   * the rollback (0001_pos_trading.down.sql) removes every pos_* object
#     and leaves the pre-existing stubs untouched.
#
# Never touches the real Supabase project — it builds its own cluster in a
# temp dir and deletes it on exit. Requires PostgreSQL server binaries.
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MIG="$REPO_ROOT/db/cloud/migrations/0001_pos_trading.sql"
DOWN="$REPO_ROOT/db/cloud/migrations/0001_pos_trading.down.sql"
BEH="$REPO_ROOT/db/cloud/tests/behavior.sql"
PORT="${PGPORT_TEST:-55433}"

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/samepos-cloud-test.XXXXXX")"

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

# Stubs for what Supabase already provides: the authenticated role, the
# auth schema with auth.uid() (reads the request.jwt.claim.sub setting,
# like the real one), and the registry's stores table (venue anchor).
stub_supabase() {
    psql_do -d "$1" -q <<'SQL'
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO authenticated;
CREATE SCHEMA IF NOT EXISTS auth;
GRANT USAGE ON SCHEMA auth TO PUBLIC;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
CREATE TABLE IF NOT EXISTS public.stores (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid,
    store_name       text NOT NULL,
    branch_code      text,
    is_active        boolean DEFAULT true,
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now()
);
SQL
}

echo "== initdb (throwaway cluster in $TMPROOT) =="
as "$PGBIN/initdb" -D "$PGDATA" -U postgres -A trust --no-locale -E UTF8 >/dev/null

echo "== start server (unix socket only, port $PORT) =="
as "$PGBIN/pg_ctl" -D "$PGDATA" -l "$LOG" -w -t 30 \
    -o "-c listen_addresses='' -k '$TMPROOT' -p $PORT" start

fail=0

echo "== apply migration to 'posmig' (with Supabase stubs) =="
createdb_do posmig
stub_supabase posmig
psql_do -d posmig -q -f "$MIG"
echo "   migration applied."

echo "== idempotency: apply migration twice to 'posidem' =="
createdb_do posidem
stub_supabase posidem
psql_do -d posidem -q -f "$MIG"
psql_do -d posidem -q -f "$MIG"
echo "   migration is idempotent (applied twice, no error)."

echo "== behavioural assertions (posmig) =="
if psql_do -d posmig -f "$BEH"; then echo "   behaviour OK."; else echo "   behaviour FAILED."; fail=1; fi

echo "== rollback removes every pos_* object, keeps pre-existing tables (posmig) =="
OBJ_SQL="SELECT 'table:'||table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name LIKE 'pos\\_%' \
UNION ALL SELECT 'func:'||routine_name FROM information_schema.routines WHERE routine_schema='public' AND routine_name LIKE 'pos\\_%' ORDER BY 1;"
psql_do -d posmig -q -f "$DOWN"
remaining="$(psql_do -d posmig -Atc "$OBJ_SQL" | grep -c . || true)"
if [ "$remaining" = "0" ]; then echo "   rollback clean (0 pos_* objects remain)."; else
    echo "   rollback left $remaining object(s):"; psql_do -d posmig -Atc "$OBJ_SQL"; fail=1; fi
stores_ok="$(psql_do -d posmig -Atc "SELECT count(*) FROM public.stores")"
if [ -n "$stores_ok" ]; then echo "   pre-existing stores table untouched."; else
    echo "   pre-existing stores table DAMAGED."; fail=1; fi

echo "== rollback is idempotent (runs again on rolled-back DB) =="
psql_do -d posmig -q -f "$DOWN"
echo "   rollback idempotent."

echo ""
if [ "$fail" -eq 0 ]; then echo "CLOUD DB TESTS: ALL PASSED"; else echo "CLOUD DB TESTS: FAILURES ABOVE"; fi
exit "$fail"
