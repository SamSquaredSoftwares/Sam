-- ===========================================================================
-- 025_monetization.down.sql  — rollback for 025_monetization.sql
-- Drops every object the monetization core creates. Safe to run repeatedly.
-- Note: this DELETES all billing state — the usage ledger, invoices, the
-- once-off charge AND the machine binding. Only run when reverting the
-- feature; back up billing_receipt_batch, billing_invoice,
-- billing_service_charge and billing_audit first if the node has real state.
-- Run this BEFORE 024_licensing.down.sql when rolling both back.
-- ===========================================================================

BEGIN;

-- Detach the install-code lock if the licensing table still exists (IF EXISTS
-- alone still errors when the table is absent, so guard on the relation).
DO $$
BEGIN
    IF to_regclass('public.licence_node') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_billing_lock_install_code ON public.licence_node';
    END IF;
END$$;

DROP VIEW     IF EXISTS billing_status;
DROP FUNCTION IF EXISTS billing_effective_status();
DROP FUNCTION IF EXISTS billing_void_invoice(bigint, text);
DROP FUNCTION IF EXISTS billing_mark_invoice_paid(bigint, text);
DROP FUNCTION IF EXISTS billing_generate_invoice(date, date);
DROP FUNCTION IF EXISTS billing_confirm_service_charge(text, bigint, text, text);
DROP FUNCTION IF EXISTS billing_bind_machine(text, text, text, text, bigint);
DROP FUNCTION IF EXISTS billing_quote_service_charge(bigint, text);
DROP FUNCTION IF EXISTS billing_record_receipt_upload(text, integer, integer, text, jsonb);
DROP FUNCTION IF EXISTS billing_machine_ok(text);
DROP FUNCTION IF EXISTS billing_lock_key(text);

-- Tables first (their triggers go with them), then the trigger functions.
DROP TABLE IF EXISTS billing_receipt_batch;
DROP TABLE IF EXISTS billing_service_charge;
DROP TABLE IF EXISTS billing_invoice;
DROP TABLE IF EXISTS billing_machine_binding;
DROP TABLE IF EXISTS billing_audit;
DROP TABLE IF EXISTS billing_config;

DROP FUNCTION IF EXISTS billing_binding_immutable();
DROP FUNCTION IF EXISTS billing_no_truncate();
DROP FUNCTION IF EXISTS billing_service_charge_guard();
DROP FUNCTION IF EXISTS billing_lock_install_code();

COMMIT;
