-- 0001_pos_trading.down.sql
-- Rollback for 0001_pos_trading.sql.
--
-- WARNING: this drops every pos_* object and with it ALL mirrored trading
-- data in the cloud (sales, lines, payments, terminals, business days,
-- products, memberships, sync runs). The venue nodes remain the source of
-- truth and are untouched; a re-applied migration can be re-filled by a
-- full re-sync. The registry tables (company_information, contact_details,
-- directors, stores) are never touched.
--
-- Idempotent: safe to run on a database where the migration was never
-- applied or was already rolled back.

BEGIN;

DROP TABLE IF EXISTS public.pos_sale_payments;
DROP TABLE IF EXISTS public.pos_sale_lines;
DROP TABLE IF EXISTS public.pos_sales;
DROP TABLE IF EXISTS public.pos_sync_runs;
DROP TABLE IF EXISTS public.pos_business_days;
DROP TABLE IF EXISTS public.pos_products;
DROP TABLE IF EXISTS public.pos_terminals;
DROP TABLE IF EXISTS public.pos_venue_members;

DROP FUNCTION IF EXISTS public.pos_is_venue_member(uuid);
DROP FUNCTION IF EXISTS public.pos_set_updated_at();

COMMIT;
