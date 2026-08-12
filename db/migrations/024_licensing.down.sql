-- ===========================================================================
-- 024_licensing.down.sql  — rollback for 024_licensing.sql
-- Drops every object the licensing core creates. Safe to run repeatedly.
-- Note: this DELETES all licence/trial state. Only run when reverting the
-- feature; back up licence + licence_audit first if the node has real state.
-- ===========================================================================

BEGIN;

-- Detach the guard trigger if its table exists (IF EXISTS alone still errors
-- when the table is absent, so guard on the relation).
DO $$
BEGIN
    IF to_regclass('public.sales_order') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_licence_guard_sales ON public.sales_order';
    END IF;
END$$;

DROP FUNCTION IF EXISTS licence_attach_guard(text);
DROP FUNCTION IF EXISTS licence_guard_sales();
DROP FUNCTION IF EXISTS licence_is_valid();
DROP VIEW     IF EXISTS licence_status;
DROP FUNCTION IF EXISTS licence_effective_status();
DROP FUNCTION IF EXISTS licence_record_signed(licence_mode, text, text, timestamptz, boolean, bigint, text, jsonb, timestamptz, text);
DROP FUNCTION IF EXISTS licence_revoke(bigint, text);
DROP FUNCTION IF EXISTS licence_ensure_trial(integer);
DROP FUNCTION IF EXISTS licence_set_node(text, text, text);

DROP TABLE IF EXISTS licence_audit;
DROP TABLE IF EXISTS licence;
DROP TABLE IF EXISTS licence_trust_key;
DROP TABLE IF EXISTS licence_node;
DROP TABLE IF EXISTS licence_config;

DROP TYPE IF EXISTS licence_mode;

COMMIT;
