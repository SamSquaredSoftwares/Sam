-- 0001_pos_trading.sql
-- SAMePOS cloud POS trading schema, as a tracked migration.
--
-- Target: the SAMePOS Supabase project (ref kpjqfsbjksnmxogmmcrb), whose
-- public schema today holds only the company registry (company_information,
-- contact_details, directors, stores). This migration adds the trading
-- mirror the sync engine pushes into. It is additive and idempotent: it
-- never drops or alters the registry tables, and it applies cleanly twice.
-- Rollback: 0001_pos_trading.down.sql (drops every object created here).
--
-- The design encodes the SAMePOS non-negotiables:
--   * Offline-first: terminals write to their venue node; the Python sync
--     engine pushes here with the SERVICE ROLE on reconnect. The cloud is a
--     mirror, not the till. Late or out-of-order pushes must not be lost,
--     so cross-row references that may not have synced yet are nullable.
--   * Idempotent sync: UNIQUE (terminal_id, sale_ref) on pos_sales is the
--     load-bearing constraint. The sync engine inserts with
--     ON CONFLICT (terminal_id, sale_ref) DO NOTHING; re-pushing a batch is
--     a safe no-op. Lines and payments carry the same property via
--     UNIQUE (sale_id, line_no) / UNIQUE (sale_id, payment_no).
--   * Money is never a float: integer cents, ZAR. VAT is 15 percent and
--     inclusive; vat_cents stores the split as the NODE computed it. The
--     cloud does not recompute VAT (node rounding is authoritative), it
--     only sanity-checks the sign and bound.
--   * RLS scoped by venue: authenticated users read only venues they are
--     members of (pos_venue_members). There are NO insert/update/delete
--     policies on purpose: all writes come from trusted backends using the
--     service role key, which bypasses RLS. Never add client write
--     policies to trading tables.
--   * POPIA: no staff or customer personal data lands in the cloud. Staff
--     appear only as staff_ref, an opaque node-side identifier.
--   * business_day race: the product once created duplicate business_day
--     rows from concurrent check-then-insert. Here the database is the
--     backstop: UNIQUE (store_id, trading_date).
--
-- Apply (any one of):
--   supabase db push            (after placing this in supabase/migrations)
--   psql -d <cloud_db> -f db/cloud/migrations/0001_pos_trading.sql
--   Supabase MCP apply_migration with this file's body
-- Test first: bash db/cloud/tests/run_cloud_db_tests.sh (disposable local
-- cluster; never touches the real project).

BEGIN;

-- ---------------------------------------------------------------------------
-- Preconditions: fail loudly on the wrong database.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.stores') IS NULL THEN
        RAISE EXCEPTION 'public.stores not found: this migration extends the '
            'SAMePOS cloud schema, where stores is the venue table. Wrong DB?';
    END IF;
    IF to_regprocedure('auth.uid()') IS NULL THEN
        RAISE EXCEPTION 'auth.uid() not found: expected a Supabase database. '
            'For local testing, run db/cloud/tests/run_cloud_db_tests.sh, '
            'which stubs auth.uid() before applying.';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Helper: keep updated_at honest on tables that carry it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.pos_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- Venue membership: who may READ a venue's trading data.
-- user_id is the Supabase auth user id. No FK to auth.users on purpose:
-- auth is a managed schema, and a dangling membership must not block user
-- deletion. Rows are written by the service role (venue onboarding).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_venue_members (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL,
    store_id    uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    member_role text NOT NULL DEFAULT 'staff'
                CHECK (member_role IN ('owner', 'manager', 'staff')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, store_id)
);

-- SECURITY DEFINER so policies on other tables can consult memberships
-- without recursing into pos_venue_members' own RLS.
CREATE OR REPLACE FUNCTION public.pos_is_venue_member(p_store_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.pos_venue_members m
         WHERE m.store_id = p_store_id
           AND m.user_id = auth.uid()
    );
$$;

-- ---------------------------------------------------------------------------
-- Terminals: one row per till at a venue. terminal_code is the node-side
-- terminal identifier; the sync engine upserts by (store_id, terminal_code)
-- and uses the returned id for sales. UNIQUE (id, store_id) exists so child
-- tables can enforce, by composite FK, that a sale's terminal belongs to
-- the same venue as the sale.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_terminals (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id      uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    terminal_code text NOT NULL,
    description   text,
    install_code  text,
    is_active     boolean NOT NULL DEFAULT true,
    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, terminal_code),
    UNIQUE (id, store_id)
);

-- ---------------------------------------------------------------------------
-- Business days: one row per venue per trading date. The unique constraint
-- is the database-level backstop for the concurrent check-then-insert race
-- the product already hit on the node.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_business_days (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id     uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    trading_date date NOT NULL,
    opened_at    timestamptz,
    closed_at    timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, trading_date),
    UNIQUE (id, store_id)
);

-- ---------------------------------------------------------------------------
-- Products: catalog mirror per venue. price_cents is VAT-inclusive (15%),
-- in integer cents, per the money rules. qty on lines is numeric to carry
-- liquor tot/pour fractions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_products (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id    uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    sku         text NOT NULL,
    name        text NOT NULL,
    category    text,
    unit        text,
    price_cents integer NOT NULL CHECK (price_cents >= 0),
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, sku)
);

-- ---------------------------------------------------------------------------
-- Sales: the core mirror row, one per sale rung on a terminal.
--   * UNIQUE (terminal_id, sale_ref) is THE idempotency anchor (rule 2).
--   * Composite FKs pin terminal and business day to the sale's own venue.
--   * business_day_id is nullable: a sale may sync before its business day
--     row does; the sync engine backfills. Never make this NOT NULL — the
--     node's NOT NULL on it is exactly gotcha #4 in the runbook, and the
--     cloud mirror must accept late arrivals rather than lose sales.
--   * total_cents may be negative only for refunds; vat_cents must lie
--     between 0 and the total (sign-aware). The exact VAT split is the
--     node's; the cloud only bounds it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_sales (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    terminal_id     uuid NOT NULL,
    business_day_id uuid,
    sale_ref        text NOT NULL,
    staff_ref       text,
    status          text NOT NULL DEFAULT 'completed'
                    CHECK (status IN ('completed', 'voided', 'refunded')),
    sold_at         timestamptz NOT NULL,
    total_cents     integer NOT NULL,
    vat_cents       integer NOT NULL,
    node_created_at timestamptz,
    synced_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (terminal_id, sale_ref),
    FOREIGN KEY (terminal_id, store_id)
        REFERENCES public.pos_terminals (id, store_id) ON DELETE RESTRICT,
    FOREIGN KEY (business_day_id, store_id)
        REFERENCES public.pos_business_days (id, store_id) ON DELETE SET NULL (business_day_id),
    CHECK (total_cents >= 0 OR status = 'refunded'),
    CHECK (vat_cents BETWEEN LEAST(total_cents, 0) AND GREATEST(total_cents, 0))
);

COMMENT ON CONSTRAINT pos_sales_terminal_id_sale_ref_key ON public.pos_sales IS
    'Load bearing: re-sync safety. The sync engine inserts with '
    'ON CONFLICT (terminal_id, sale_ref) DO NOTHING. Never remove or widen.';

CREATE INDEX IF NOT EXISTS pos_sales_store_sold_at_idx
    ON public.pos_sales (store_id, sold_at DESC);
CREATE INDEX IF NOT EXISTS pos_sales_business_day_idx
    ON public.pos_sales (business_day_id);

-- ---------------------------------------------------------------------------
-- Sale lines: snapshot of what was sold. description/sku are copied at sale
-- time (catalog rows change; history must not). product_id is nullable for
-- late-syncing catalogs, ON DELETE SET NULL keeps history if a product goes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_sale_lines (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sale_id          uuid NOT NULL REFERENCES public.pos_sales(id) ON DELETE CASCADE,
    line_no          integer NOT NULL CHECK (line_no > 0),
    product_id       uuid REFERENCES public.pos_products(id) ON DELETE SET NULL,
    sku              text,
    description      text NOT NULL,
    qty              numeric(12,3) NOT NULL CHECK (qty <> 0),
    unit_price_cents integer NOT NULL,
    line_total_cents integer NOT NULL,
    vat_cents        integer NOT NULL,
    UNIQUE (sale_id, line_no)
);

-- ---------------------------------------------------------------------------
-- Sale payments: how the sale was settled. Split payments are multiple rows.
-- tendered/change support cash reconciliation at cash-up.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_sale_payments (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sale_id        uuid NOT NULL REFERENCES public.pos_sales(id) ON DELETE CASCADE,
    payment_no     integer NOT NULL CHECK (payment_no > 0),
    method         text NOT NULL
                   CHECK (method IN ('cash', 'card', 'eft', 'account', 'voucher', 'other')),
    amount_cents   integer NOT NULL,
    tendered_cents integer,
    change_cents   integer,
    UNIQUE (sale_id, payment_no)
);

-- ---------------------------------------------------------------------------
-- Sync runs: audit trail of sync-engine batches, for debugging duplicate or
-- lost-sale reports (starter prompt 1). Written by the service role only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.pos_sync_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id     uuid NOT NULL REFERENCES public.stores(id) ON DELETE CASCADE,
    terminal_id  uuid REFERENCES public.pos_terminals(id) ON DELETE SET NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'failed')),
    sales_pushed integer NOT NULL DEFAULT 0,
    error        text
);

-- ---------------------------------------------------------------------------
-- updated_at triggers (guarded for idempotency).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgname = 'pos_terminals_set_updated_at'
                     AND tgrelid = 'public.pos_terminals'::regclass) THEN
        CREATE TRIGGER pos_terminals_set_updated_at
            BEFORE UPDATE ON public.pos_terminals
            FOR EACH ROW EXECUTE FUNCTION public.pos_set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgname = 'pos_products_set_updated_at'
                     AND tgrelid = 'public.pos_products'::regclass) THEN
        CREATE TRIGGER pos_products_set_updated_at
            BEFORE UPDATE ON public.pos_products
            FOR EACH ROW EXECUTE FUNCTION public.pos_set_updated_at();
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Row Level Security. Read-only for venue members; NO write policies —
-- writes are service-role only (rule 6). DROP+CREATE keeps this idempotent.
-- ---------------------------------------------------------------------------
ALTER TABLE public.pos_venue_members  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_terminals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_business_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_products      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_sales         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_sale_lines    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_sale_payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pos_sync_runs     ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Members read own memberships" ON public.pos_venue_members;
CREATE POLICY "Members read own memberships"
    ON public.pos_venue_members FOR SELECT TO authenticated
    USING (user_id = auth.uid());

DROP POLICY IF EXISTS "Venue members read terminals" ON public.pos_terminals;
CREATE POLICY "Venue members read terminals"
    ON public.pos_terminals FOR SELECT TO authenticated
    USING (public.pos_is_venue_member(store_id));

DROP POLICY IF EXISTS "Venue members read business days" ON public.pos_business_days;
CREATE POLICY "Venue members read business days"
    ON public.pos_business_days FOR SELECT TO authenticated
    USING (public.pos_is_venue_member(store_id));

DROP POLICY IF EXISTS "Venue members read products" ON public.pos_products;
CREATE POLICY "Venue members read products"
    ON public.pos_products FOR SELECT TO authenticated
    USING (public.pos_is_venue_member(store_id));

DROP POLICY IF EXISTS "Venue members read sales" ON public.pos_sales;
CREATE POLICY "Venue members read sales"
    ON public.pos_sales FOR SELECT TO authenticated
    USING (public.pos_is_venue_member(store_id));

DROP POLICY IF EXISTS "Venue members read sale lines" ON public.pos_sale_lines;
CREATE POLICY "Venue members read sale lines"
    ON public.pos_sale_lines FOR SELECT TO authenticated
    USING (EXISTS (SELECT 1 FROM public.pos_sales s
                   WHERE s.id = sale_id
                     AND public.pos_is_venue_member(s.store_id)));

DROP POLICY IF EXISTS "Venue members read sale payments" ON public.pos_sale_payments;
CREATE POLICY "Venue members read sale payments"
    ON public.pos_sale_payments FOR SELECT TO authenticated
    USING (EXISTS (SELECT 1 FROM public.pos_sales s
                   WHERE s.id = sale_id
                     AND public.pos_is_venue_member(s.store_id)));

DROP POLICY IF EXISTS "Venue members read sync runs" ON public.pos_sync_runs;
CREATE POLICY "Venue members read sync runs"
    ON public.pos_sync_runs FOR SELECT TO authenticated
    USING (public.pos_is_venue_member(store_id));

-- RLS is the enforcement; grants just open the SELECT path for members.
-- anon gets nothing: trading data is never public.
GRANT SELECT ON public.pos_venue_members, public.pos_terminals,
                public.pos_business_days, public.pos_products,
                public.pos_sales, public.pos_sale_lines,
                public.pos_sale_payments, public.pos_sync_runs
    TO authenticated;

COMMIT;
