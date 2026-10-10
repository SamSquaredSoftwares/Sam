-- behavior.sql — behavioural assertions for the cloud POS trading schema.
-- Run by run_cloud_db_tests.sh against a disposable cluster where the
-- migration has been applied. Superuser stands in for the service role
-- (which bypasses RLS on Supabase); SET LOCAL ROLE authenticated exercises
-- the venue-scoped read path. Every failure raises, so psql -v
-- ON_ERROR_STOP=1 turns any regression into a non-zero exit.

-- ---------------------------------------------------------------------------
-- Seed (as the service-role stand-in): two venues, one member, one
-- terminal, business day, product, and one synced sale.
-- ---------------------------------------------------------------------------
INSERT INTO public.stores (id, store_name)
VALUES ('00000000-0000-0000-0000-00000000000a', 'Booth Liquor'),
       ('00000000-0000-0000-0000-00000000000b', 'Venue Two');

INSERT INTO public.pos_venue_members (user_id, store_id, member_role)
VALUES ('00000000-0000-0000-0000-000000000101'::uuid,
        '00000000-0000-0000-0000-00000000000a', 'owner');

INSERT INTO public.pos_terminals (id, store_id, terminal_code)
VALUES ('00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-00000000000a', 'TILL-01');

INSERT INTO public.pos_business_days (id, store_id, trading_date, opened_at)
VALUES ('00000000-0000-0000-0000-000000000301',
        '00000000-0000-0000-0000-00000000000a', '2026-09-12', now());

INSERT INTO public.pos_products (id, store_id, sku, name, price_cents)
VALUES ('00000000-0000-0000-0000-000000000401',
        '00000000-0000-0000-0000-00000000000a', 'BRDY-750', 'Brandy 750ml', 24999);

INSERT INTO public.pos_sales
    (id, store_id, terminal_id, business_day_id, sale_ref, staff_ref,
     sold_at, total_cents, vat_cents)
VALUES
    ('00000000-0000-0000-0000-000000000501',
     '00000000-0000-0000-0000-00000000000a',
     '00000000-0000-0000-0000-000000000201',
     '00000000-0000-0000-0000-000000000301',
     'S-000123', 'staff-7', now(), 24999, 3261);

INSERT INTO public.pos_sale_lines
    (sale_id, line_no, product_id, sku, description, qty,
     unit_price_cents, line_total_cents, vat_cents)
VALUES ('00000000-0000-0000-0000-000000000501', 1,
        '00000000-0000-0000-0000-000000000401', 'BRDY-750', 'Brandy 750ml',
        1, 24999, 24999, 3261);

INSERT INTO public.pos_sale_payments
    (sale_id, payment_no, method, amount_cents, tendered_cents, change_cents)
VALUES ('00000000-0000-0000-0000-000000000501', 1, 'cash', 24999, 25000, 1);

-- ---------------------------------------------------------------------------
-- 1. Idempotent re-sync: re-pushing the same sale is a safe no-op.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n integer;
BEGIN
    INSERT INTO public.pos_sales
        (store_id, terminal_id, business_day_id, sale_ref, sold_at,
         total_cents, vat_cents)
    VALUES
        ('00000000-0000-0000-0000-00000000000a',
         '00000000-0000-0000-0000-000000000201',
         '00000000-0000-0000-0000-000000000301',
         'S-000123', now(), 24999, 3261)
    ON CONFLICT (terminal_id, sale_ref) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    ASSERT n = 0, 're-push inserted a duplicate sale';
    SELECT count(*) INTO n FROM public.pos_sales WHERE sale_ref = 'S-000123';
    ASSERT n = 1, format('expected 1 sale for S-000123, found %s', n);
    RAISE NOTICE 'ok 1: re-sync of the same (terminal_id, sale_ref) is a no-op';
END $$;

-- ---------------------------------------------------------------------------
-- 2. A blind duplicate insert (no ON CONFLICT) is rejected outright.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO public.pos_sales
            (store_id, terminal_id, sale_ref, sold_at, total_cents, vat_cents)
        VALUES ('00000000-0000-0000-0000-00000000000a',
                '00000000-0000-0000-0000-000000000201',
                'S-000123', now(), 100, 13);
        RAISE EXCEPTION 'duplicate (terminal_id, sale_ref) was accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok 2: duplicate (terminal_id, sale_ref) rejected';
    END;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Duplicate business day for the same venue and date is rejected
--    (the check-then-insert race, killed at the database).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO public.pos_business_days (store_id, trading_date)
        VALUES ('00000000-0000-0000-0000-00000000000a', '2026-09-12');
        RAISE EXCEPTION 'duplicate business day was accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok 3: duplicate (store_id, trading_date) rejected';
    END;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Money rules: no negative totals outside refunds; VAT bounded by the
--    total, sign-aware; a refund with negative total and VAT is accepted.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO public.pos_sales
            (store_id, terminal_id, sale_ref, sold_at, total_cents, vat_cents)
        VALUES ('00000000-0000-0000-0000-00000000000a',
                '00000000-0000-0000-0000-000000000201',
                'S-BAD-NEG', now(), -100, 0);
        RAISE EXCEPTION 'negative total on a completed sale was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 4a: negative total on completed sale rejected';
    END;
    BEGIN
        INSERT INTO public.pos_sales
            (store_id, terminal_id, sale_ref, sold_at, total_cents, vat_cents)
        VALUES ('00000000-0000-0000-0000-00000000000a',
                '00000000-0000-0000-0000-000000000201',
                'S-BAD-VAT', now(), 100, 200);
        RAISE EXCEPTION 'vat_cents above total_cents was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 4b: vat_cents above total_cents rejected';
    END;
    INSERT INTO public.pos_sales
        (store_id, terminal_id, sale_ref, status, sold_at,
         total_cents, vat_cents)
    VALUES ('00000000-0000-0000-0000-00000000000a',
            '00000000-0000-0000-0000-000000000201',
            'S-000124-REF', 'refunded', now(), -24999, -3261);
    RAISE NOTICE 'ok 4c: refund with negative total and VAT accepted';
END $$;

-- ---------------------------------------------------------------------------
-- 5. Cross-venue integrity: a sale cannot claim venue B while ringing on
--    venue A's terminal (composite FK).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO public.pos_sales
            (store_id, terminal_id, sale_ref, sold_at, total_cents, vat_cents)
        VALUES ('00000000-0000-0000-0000-00000000000b',
                '00000000-0000-0000-0000-000000000201',
                'S-CROSS', now(), 100, 13);
        RAISE EXCEPTION 'cross-venue terminal reference was accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'ok 5: cross-venue (terminal, store) mismatch rejected';
    END;
END $$;

-- ---------------------------------------------------------------------------
-- 6. Line and payment re-push idempotency: duplicates rejected, ON
--    CONFLICT DO NOTHING is a no-op.
-- ---------------------------------------------------------------------------
DO $$
DECLARE n integer;
BEGIN
    INSERT INTO public.pos_sale_lines
        (sale_id, line_no, description, qty, unit_price_cents,
         line_total_cents, vat_cents)
    VALUES ('00000000-0000-0000-0000-000000000501', 1, 'Brandy 750ml', 1,
            24999, 24999, 3261)
    ON CONFLICT (sale_id, line_no) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    ASSERT n = 0, 're-pushed sale line was inserted twice';
    INSERT INTO public.pos_sale_payments
        (sale_id, payment_no, method, amount_cents)
    VALUES ('00000000-0000-0000-0000-000000000501', 1, 'cash', 24999)
    ON CONFLICT (sale_id, payment_no) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    ASSERT n = 0, 're-pushed sale payment was inserted twice';
    RAISE NOTICE 'ok 6: line and payment re-push are no-ops';
END $$;

-- ---------------------------------------------------------------------------
-- 7. updated_at trigger fires on product updates.
-- ---------------------------------------------------------------------------
DO $$
DECLARE before_ts timestamptz; after_ts timestamptz;
BEGIN
    SELECT updated_at INTO before_ts FROM public.pos_products
     WHERE id = '00000000-0000-0000-0000-000000000401';
    PERFORM pg_sleep(0.01);
    UPDATE public.pos_products SET name = 'Brandy 750ml (renamed)'
     WHERE id = '00000000-0000-0000-0000-000000000401';
    SELECT updated_at INTO after_ts FROM public.pos_products
     WHERE id = '00000000-0000-0000-0000-000000000401';
    ASSERT after_ts > before_ts, 'updated_at did not advance on UPDATE';
    RAISE NOTICE 'ok 7: updated_at trigger fires';
END $$;

-- ---------------------------------------------------------------------------
-- 8. RLS: a venue member reads only their venue; a stranger reads nothing;
--    nobody writes without the service role.
-- ---------------------------------------------------------------------------
BEGIN;
SELECT set_config('request.jwt.claim.sub',
                  '00000000-0000-0000-0000-000000000101', true);
SET LOCAL ROLE authenticated;
DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM public.pos_sales;
    ASSERT n = 2, format('member should see 2 sales at their venue, saw %s', n);
    SELECT count(*) INTO n FROM public.pos_venue_members;
    ASSERT n = 1, format('member should see only their own membership, saw %s', n);
    SELECT count(*) INTO n FROM public.pos_sale_lines;
    ASSERT n = 1, format('member should see 1 sale line, saw %s', n);
    SELECT count(*) INTO n FROM public.pos_terminals;
    ASSERT n = 1, format('member should see 1 terminal, saw %s', n);
    BEGIN
        INSERT INTO public.pos_sales
            (store_id, terminal_id, sale_ref, sold_at, total_cents, vat_cents)
        VALUES ('00000000-0000-0000-0000-00000000000a',
                '00000000-0000-0000-0000-000000000201',
                'S-CLIENT-WRITE', now(), 100, 13);
        RAISE EXCEPTION 'authenticated client wrote a sale (writes must be service-role only)';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'ok 8a: authenticated writes are refused';
    END;
    RAISE NOTICE 'ok 8b: venue member sees exactly their venue''s data';
END $$;
ROLLBACK;

BEGIN;
SELECT set_config('request.jwt.claim.sub',
                  '00000000-0000-0000-0000-000000000102', true);
SET LOCAL ROLE authenticated;
DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM public.pos_sales;
    ASSERT n = 0, format('non-member saw %s sales', n);
    SELECT count(*) INTO n FROM public.pos_venue_members;
    ASSERT n = 0, format('non-member saw %s memberships', n);
    SELECT count(*) INTO n FROM public.pos_products;
    ASSERT n = 0, format('non-member saw %s products', n);
    RAISE NOTICE 'ok 8c: non-member sees nothing';
END $$;
ROLLBACK;

SELECT 'ALL BEHAVIOUR ASSERTIONS PASSED' AS result;
