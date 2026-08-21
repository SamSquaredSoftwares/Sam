-- ===========================================================================
-- check_reconciliation.sql — READ-ONLY pre-flight for the licensing core.
--
-- Run this against a real SAMePOS node database BEFORE applying
-- db/migrations/024_licensing.sql. It reports everything needed to reconcile
-- the licensing core with whatever the product's own migrations (001-023)
-- already created, and to pick the right guard attachment point.
--
--     psql -d <node_db> -f db/tools/check_reconciliation.sql > reconcile.txt
--
-- SAFETY: this script only reads system catalogs and counts rows. It creates,
-- alters, deletes and updates nothing, and is safe on a live trading node.
-- Send the output back (or read it yourself) to complete the reconciliation.
-- ===========================================================================
\pset pager off
\set ON_ERROR_STOP on

\echo ''
\echo '=============================================================='
\echo ' SAMePOS licensing core — reconciliation pre-flight (READ ONLY)'
\echo '=============================================================='

\echo ''
\echo '--- 1. Server / database identity -----------------------------'
SELECT current_database() AS database,
       current_schema()   AS schema,
       version()          AS server_version;

\echo ''
\echo '--- 2. Existing licence-ish objects already in this DB --------'
\echo '    (anything here means RECONCILE BEFORE APPLYING: the'
\echo '     migration''s CREATE ... IF NOT EXISTS would adopt it)'
SELECT CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'matview' WHEN 'i' THEN 'index'
                      WHEN 'S' THEN 'sequence' WHEN 'p' THEN 'partitioned table'
                      ELSE c.relkind::text END AS object_type,
       n.nspname AS schema, c.relname AS name
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema')
   AND (c.relname ILIKE '%licen%' OR c.relname ILIKE '%trial%')
UNION ALL
SELECT 'function', n.nspname, p.proname
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema')
   AND (p.proname ILIKE '%licen%' OR p.proname ILIKE '%trial%')
UNION ALL
SELECT 'type', n.nspname, t.typname
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema')
   AND (t.typname ILIKE '%licen%' OR t.typname ILIKE '%trial%')
 ORDER BY 1, 3;

\echo ''
\echo '--- 3. Columns of any existing licence-ish tables -------------'
SELECT c.table_name, c.ordinal_position AS pos, c.column_name,
       c.data_type, c.is_nullable, c.column_default
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
 WHERE c.table_schema NOT IN ('pg_catalog','information_schema')
   AND t.table_type = 'BASE TABLE'
   AND (c.table_name ILIKE '%licen%' OR c.table_name ILIKE '%trial%')
 ORDER BY c.table_name, c.ordinal_position;

\echo ''
\echo '--- 4. Direct collision check against the 024 object names ----'
WITH wanted(kind, name) AS (VALUES
    ('type',     'licence_mode'),
    ('table',    'licence_config'),
    ('table',    'licence_node'),
    ('table',    'licence_trust_key'),
    ('table',    'licence'),
    ('table',    'licence_audit'),
    ('view',     'licence_status'),
    ('function', 'licence_set_node'),
    ('function', 'licence_ensure_trial'),
    ('function', 'licence_record_signed'),
    ('function', 'licence_revoke'),
    ('function', 'licence_effective_status'),
    ('function', 'licence_is_valid'),
    ('function', 'licence_guard_sales'),
    ('function', 'licence_attach_guard'),
    ('function', 'licence_attach_guard_auto')
)
SELECT w.kind, w.name,
       CASE WHEN w.kind = 'function' THEN
                 EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                          WHERE n.nspname='public' AND p.proname = w.name)
            WHEN w.kind = 'type' THEN
                 EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                          WHERE n.nspname='public' AND t.typname = w.name)
            ELSE to_regclass('public.'||w.name) IS NOT NULL
       END AS already_exists
  FROM wanted w
 ORDER BY w.kind, w.name;

\echo ''
\echo '--- 5. Migration tracking table (to confirm the next number) --'
SELECT n.nspname AS schema, c.relname AS candidate_table
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema')
   AND (c.relname ILIKE '%migration%' OR c.relname ILIKE '%schema_version%'
        OR c.relname ILIKE '%alembic%' OR c.relname ILIKE '%flyway%')
 ORDER BY 1, 2;

DO $$
DECLARE t text; r record; n bigint;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS tbl
          FROM pg_class c JOIN pg_namespace n2 ON n2.oid = c.relnamespace
         WHERE c.relkind='r' AND n2.nspname NOT IN ('pg_catalog','information_schema')
           AND (c.relname ILIKE '%migration%' OR c.relname ILIKE '%schema_version%'
                OR c.relname ILIKE '%alembic%' OR c.relname ILIKE '%flyway%')
    LOOP
        EXECUTE format('SELECT count(*) FROM %s', r.tbl) INTO n;
        RAISE NOTICE 'migration table % has % row(s)', r.tbl, n;
    END LOOP;
END$$;

\echo ''
\echo '--- 6. Guard attachment point: sales / payment tables ---------'
\echo '    (the hard guard must attach to the PAYMENT insert)'
SELECT n.nspname AS schema, c.relname AS table_name
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema')
   AND (c.relname ILIKE '%payment%' OR c.relname ILIKE '%tender%'
        OR c.relname ILIKE 'sales%'  OR c.relname ILIKE '%order%')
 ORDER BY 1, 2;

\echo ''
\echo '    Which of the auto-probe candidates exist?'
WITH cand(t) AS (VALUES ('public.sales_payment'),('public.sale_payment'),
                        ('public.order_payment'),('public.payment'))
SELECT t AS candidate, to_regclass(t) IS NOT NULL AS exists FROM cand ORDER BY 1;

\echo ''
\echo '--- 7. Pre-existing licence/trial STATE (row counts only) -----'
\echo '    (non-zero means migrate the state in; do NOT let a fresh'
\echo '     trial start and silently extend the term)'
DO $$
DECLARE r record; n bigint;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS tbl
          FROM pg_class c JOIN pg_namespace nn ON nn.oid = c.relnamespace
         WHERE c.relkind='r' AND nn.nspname NOT IN ('pg_catalog','information_schema')
           AND (c.relname ILIKE '%licen%' OR c.relname ILIKE '%trial%')
    LOOP
        EXECUTE format('SELECT count(*) FROM %s', r.tbl) INTO n;
        RAISE NOTICE '% -> % row(s)', r.tbl, n;
    END LOOP;
    IF NOT FOUND THEN
        RAISE NOTICE 'no licence/trial tables present';
    END IF;
END$$;

\echo ''
\echo '--- 8. Existing guard trigger, if any -------------------------'
SELECT c.oid::regclass AS on_table, t.tgname AS trigger_name
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
 WHERE NOT t.tgisinternal AND t.tgname ILIKE '%licence%'
 ORDER BY 1;

\echo ''
\echo '=============================================================='
\echo ' Done. Nothing was modified. Send this output back to complete'
\echo ' the reconciliation (or read section 4: any TRUE = collision).'
\echo '=============================================================='
\echo ''
