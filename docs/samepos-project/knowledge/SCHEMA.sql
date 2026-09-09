-- SCHEMA.sql
-- STATUS: CLOUD HALF DONE. Local node half still to dump (see bottom).
--
-- Knowledge file #2 of the SAMePOS Claude Project pack.
--
-- Source: the live SAMePOS Supabase project.
--   Project ref: kpjqfsbjksnmxogmmcrb (eu-west-1, Postgres 17.6.1).
--   Extracted:   25 Aug 2026, via read-only catalog queries over the
--                Supabase management API (equivalent of
--                pg_dump --schema-only for the public schema).
--   The project was paused; it was restored on 25 Aug 2026 to take this
--   dump and left ACTIVE. Pause it again in the dashboard if you want.
--
-- THE ONE THING THAT MATTERS: THE CLOUD HAS NO POS TABLES YET.
--
-- The public schema below is a company registry only: company identity,
-- contacts, directors, stores. There are NO sales, products, terminals,
-- payments, or sync tables in the cloud. The (terminal_id, sale_ref)
-- unique constraint does not exist here. The POS trading schema lives
-- only on the local nodes (PostgreSQL on port 5433, migrations 001-023)
-- and in the licensing core (db/schema/licensing.schema.sql in the Sam
-- repo). Until the local dump below is added, Claude must not assume
-- any cloud table it cannot see in this file.
--
-- Other verified facts about this project, 25 Aug 2026:
-- - No Edge Functions are deployed. The AI invoice-scanning function
--   from the pack's stack facts is not in this project yet.
-- - No tracked migrations. These tables were created outside the
--   migration system (likely the dashboard SQL editor).
-- - RLS is ENABLED on all four tables. The only policies are read-only
--   SELECT for the authenticated role. There are no INSERT, UPDATE, or
--   DELETE policies, so writes are only possible with the service role
--   key, which bypasses RLS. That matches non-negotiable rule 6.
-- - Supabase-managed schemas exist and are excluded from this dump:
--   auth (23 tables), storage (8), realtime (2), cron (2), net (2),
--   supabase_functions (2), vault (1), extensions, graphql,
--   graphql_public, pgmq.
-- - Installed extensions: pg_cron 1.6.4, pg_graphql 1.6.1, pg_net
--   0.20.3, pg_stat_statements 1.11, pgcrypto 1.3, pgmq 1.5.1, plpgsql
--   1.0, supabase_vault 0.3.1, uuid-ossp 1.1, wrappers 0.6.2.
--
-- This file is executable: applying it to an empty database that has an
-- `authenticated` role recreates the public schema exactly.

-- ============================================================
-- Tables
-- ============================================================

CREATE TABLE public.company_information (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name          text NOT NULL,
    registration_number text NOT NULL,
    enterprise_type     text,
    enterprise_status   text,
    registration_date   date,
    business_start_date date,
    financial_year_end  text,
    moi_type            text,
    main_object         text,
    postal_address      text,
    registered_address  text,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now()
);

CREATE TABLE public.contact_details (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    uuid REFERENCES public.company_information(id) ON DELETE CASCADE,
    contact_type  text,
    contact_value text,
    label         text,
    created_at    timestamptz DEFAULT now()
);

CREATE TABLE public.directors (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid REFERENCES public.company_information(id) ON DELETE CASCADE,
    full_name        text NOT NULL,
    role             text DEFAULT 'DIRECTOR'::text,
    status           text DEFAULT 'ACTIVE'::text,
    appointment_date date,
    created_at       timestamptz DEFAULT now()
);

CREATE TABLE public.stores (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid REFERENCES public.company_information(id) ON DELETE CASCADE,
    store_name       text NOT NULL,
    branch_code      text,
    phone            text,
    email            text,
    physical_address text,
    is_active        boolean DEFAULT true,
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now()
);

-- No custom functions, triggers, views, custom types, sequences, or
-- non-PK indexes exist in the public schema. (Verified 25 Aug 2026.)

-- ============================================================
-- Row Level Security
-- ============================================================
-- All four tables: RLS on, SELECT-only for authenticated, no write
-- policies (writes go through the service role, which bypasses RLS).

ALTER TABLE public.company_information ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contact_details     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.directors           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stores              ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Authenticated users can read company info"
    ON public.company_information FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated read contact_details"
    ON public.contact_details FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated read directors"
    ON public.directors FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated read stores"
    ON public.stores FOR SELECT TO authenticated USING (true);

-- ============================================================
-- Observations for Claude (not DDL)
-- ============================================================
-- 1. registration_number has no unique constraint. Two rows can hold
--    the same company registration. Decide if that is intended.
-- 2. company_id is nullable on contact_details, directors, and stores.
--    Orphan rows (no company) are possible.
-- 3. The RLS read policies expose ALL rows to ANY authenticated user.
--    There is no per-venue scoping yet. The pack's rule "RLS scoped by
--    venue" is not implemented in the cloud today.
-- 4. updated_at has no trigger; it only changes if the writer sets it.
-- 5. POPIA: directors.full_name, stores.phone, and stores.email are
--    personal data. Minimum collection and no plain-text logging apply.

-- ============================================================
-- STILL TO DO: the local node schema (the POS trading tables)
-- ============================================================
-- The real POS schema (sales, products, terminals, payments, sync,
-- business days, migrations 001-023) lives on each venue node's local
-- PostgreSQL on port 5433. Dump it on a node and append it here, or
-- keep it as SCHEMA_LOCAL.sql alongside this file:
--
--   pg_dump --schema-only --no-owner --no-privileges \
--     -h localhost -p 5433 -U postgres -d <node_db> \
--     > SCHEMA_LOCAL.sql
--
-- You will be prompted for the password. Never paste it into any file.
-- Skim the output before uploading. The licensing slice of the node
-- schema is already in the Sam repo at db/schema/licensing.schema.sql.
