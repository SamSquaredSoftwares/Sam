-- ===========================================================================
-- behavior.sql — behavioural assertions for the SAMePOS licensing core.
-- Run against a database that has 024_licensing.sql applied AND a payment
-- stub/table present (so the hard-guard trigger is attached). Uses plpgsql
-- ASSERT; any failure aborts under psql -v ON_ERROR_STOP=1.
-- ===========================================================================
\set ON_ERROR_STOP on

-- Guard trigger must be attached, and specifically to the PAYMENT table --
-- enforcement happens at payment, not at tab-open, so an expired licence can
-- never strand an open shift.
DO $$
DECLARE v_tbl text;
BEGIN
    SELECT c.relname INTO v_tbl
      FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
     WHERE t.tgname = 'trg_licence_guard_sales' AND NOT t.tgisinternal;
    ASSERT v_tbl IS NOT NULL, 'hard-guard trigger was not attached';
    ASSERT v_tbl = 'sales_payment',
        'guard must attach to the payment table, got '||v_tbl;
    ASSERT NOT EXISTS (
        SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE t.tgname = 'trg_licence_guard_sales' AND c.relname = 'sales_order'),
        'guard must NOT block tab-open (sales_order)';
END$$;

-- 1. No node identity yet -> state 'none', invalid.
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'none',   'expected none, got '||s.state;
    ASSERT s.is_valid = false, 'expected invalid with no node';
END$$;

-- 2. Set node + auto-start trial -> valid ~14 days; idempotent.
SELECT licence_set_node('INSTALL-AAA-0001', '1.0.0', 'till-1');
SELECT licence_ensure_trial();
DO $$
DECLARE s record; n int;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'trial', 'expected trial, got '||s.state;
    ASSERT s.is_valid,        'trial should be valid';
    ASSERT s.days_remaining BETWEEN 13 AND 14,
        'trial days_remaining ~14, got '||coalesce(s.days_remaining::text,'null');
    PERFORM licence_ensure_trial();     -- second call must not add another trial
    SELECT count(*) INTO n FROM licence WHERE mode = 'trial';
    ASSERT n = 1, 'ensure_trial must be idempotent; found '||n||' trials';
END$$;

-- 3. Expire the trial -> state 'expired', invalid, 0 days.
UPDATE licence SET starts_at = now() - interval '20 days',
                   expires_at = now() - interval '6 days'
 WHERE mode = 'trial';
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'expired', 'expected expired, got '||s.state;
    ASSERT NOT s.is_valid,      'expired trial must be invalid';
    ASSERT s.days_remaining = 0,'expired days_remaining should be 0, got '||coalesce(s.days_remaining::text,'null');
END$$;

-- 4. Grace window -> valid + in_grace; turning grace off -> expired again.
UPDATE licence_config SET grace_days = 3 WHERE id = 1;
UPDATE licence SET expires_at = now() - interval '1 day' WHERE mode = 'trial';
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'grace', 'expected grace, got '||s.state;
    ASSERT s.is_valid,        'grace should still be valid';
    ASSERT s.in_grace,        'in_grace flag expected';
END$$;
UPDATE licence_config SET grace_days = 0 WHERE id = 1;
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'expired', 'grace off -> expected expired, got '||s.state;
END$$;

-- 5. Signed licence rejected (success=false) when the app did not verify it.
DO $$
DECLARE ok boolean; rsn text;
BEGIN
    SELECT success, reason INTO ok, rsn
      FROM licence_record_signed('licensed','SPOS1.KEYA','INSTALL-AAA-0001',NULL,false);
    ASSERT ok = false, 'unverified licence must be rejected';
    ASSERT rsn ILIKE '%verif%', 'reason should mention verification, got: '||coalesce(rsn,'null');
    ASSERT NOT EXISTS (SELECT 1 FROM licence WHERE key_string='SPOS1.KEYA'),
        'a rejected licence must NOT be stored';
END$$;

-- 6. Signed licence rejected when bound to a different install code.
DO $$
DECLARE ok boolean; rsn text;
BEGIN
    SELECT success, reason INTO ok, rsn
      FROM licence_record_signed('licensed','SPOS1.KEYB','WRONG-INSTALL',NULL,true);
    ASSERT ok = false, 'mismatched-binding licence must be rejected';
    ASSERT rsn ILIKE '%install code mismatch%', 'reason should mention mismatch, got: '||coalesce(rsn,'null');
    ASSERT NOT EXISTS (SELECT 1 FROM licence WHERE key_string='SPOS1.KEYB'),
        'a rejected licence must NOT be stored';
END$$;
-- both rejects are persisted in the audit trail (they no longer roll back)
DO $$
DECLARE c int;
BEGIN
    SELECT count(*) INTO c FROM licence_audit WHERE action = 'reject';
    ASSERT c >= 2, 'expected >=2 reject audit rows, got '||c;
END$$;

-- 7. Perpetual licensed accepted; outranks the expired trial.
DO $$
DECLARE ok boolean; lid bigint;
BEGIN
    SELECT success, licence_id INTO ok, lid
      FROM licence_record_signed('licensed','SPOS1.KEYC','INSTALL-AAA-0001',
                                 NULL, true, NULL, 'sigC', '{"features":["all"]}'::jsonb);
    ASSERT ok, 'valid licensed activation must succeed';
    ASSERT lid IS NOT NULL, 'licence_id expected on success';
END$$;
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'licensed',      'expected licensed, got '||s.state;
    ASSERT s.is_valid,                'licensed must be valid';
    ASSERT s.expires_at IS NULL,      'perpetual licence has null expiry';
    ASSERT s.days_remaining IS NULL,  'perpetual days_remaining must be null';
END$$;

-- 8. Idempotent by key fingerprint: same key twice -> one row.
DO $$
DECLARE ok boolean;
BEGIN
    SELECT success INTO ok
      FROM licence_record_signed('licensed','SPOS1.KEYC','INSTALL-AAA-0001',NULL,true);
    ASSERT ok, 're-recording the same key should still succeed';
END$$;
DO $$
DECLARE c int;
BEGIN
    SELECT count(*) INTO c FROM licence WHERE key_string = 'SPOS1.KEYC';
    ASSERT c = 1, 'expected 1 row for KEYC, got '||c;
END$$;

-- 9. Add a 30-day extended trial; revoke the licensed -> falls back to it.
DO $$
DECLARE ok boolean;
BEGIN
    SELECT success INTO ok
      FROM licence_record_signed('extended_trial','SPOS1.KEYD','INSTALL-AAA-0001',
                                 now() + interval '30 days', true);
    ASSERT ok, 'extended trial activation must succeed';
END$$;
DO $$
DECLARE lid bigint;
BEGIN
    SELECT id INTO lid FROM licence
     WHERE mode='licensed' AND revoked_at IS NULL ORDER BY id DESC LIMIT 1;
    PERFORM licence_revoke(lid, 'test: superseded');
END$$;
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT s.state = 'extended_trial',
        'after revoking licensed, expected extended_trial, got '||s.state;
    ASSERT s.is_valid, 'extended_trial must be valid';
    ASSERT s.days_remaining BETWEEN 29 AND 30,
        'extended trial ~30d, got '||coalesce(s.days_remaining::text,'null');
END$$;

-- 10. Hard guard behaviour (enforcement point = payment).
-- 10a. Valid licence -> payment allowed even with hard enforcement ON.
UPDATE licence_config SET hard_enforcement = true WHERE id = 1;
INSERT INTO sales_order (business_day_id) VALUES (1);
INSERT INTO sales_payment (sales_order_id, amount) VALUES (1, 10.00);

-- 10b. Revoke the extended trial -> node now invalid (only expired trial left).
DO $$
DECLARE lid bigint;
BEGIN
    SELECT id INTO lid FROM licence
     WHERE mode='extended_trial' AND revoked_at IS NULL ORDER BY id DESC LIMIT 1;
    PERFORM licence_revoke(lid, 'test: end');
END$$;
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM licence_status;
    ASSERT NOT s.is_valid, 'expected invalid after revoking all valid licences, state '||s.state;
END$$;

-- 10c. hard_enforcement ON + invalid -> PAYMENT blocked.
DO $$
BEGIN
    BEGIN
        INSERT INTO sales_payment (sales_order_id, amount) VALUES (1, 20.00);
        RAISE EXCEPTION 'SENTINEL: expected the guard to block this payment';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'hard guard did NOT block a payment while invalid';
    END;
END$$;

-- 10c-bis. ...but opening/continuing a tab still works, so an expired licence
-- never strands an open shift mid-service.
INSERT INTO sales_order (business_day_id) VALUES (2);
DO $$
DECLARE c int;
BEGIN
    SELECT count(*) INTO c FROM sales_order;
    ASSERT c = 2, 'tab-open must remain allowed while invalid; got '||c||' orders';
END$$;

-- 10d. Advisory only (hard_enforcement OFF) + invalid -> payment ALLOWED.
UPDATE licence_config SET hard_enforcement = false WHERE id = 1;
INSERT INTO sales_payment (sales_order_id, amount) VALUES (2, 30.00);
DO $$
DECLARE c int;
BEGIN
    SELECT count(*) INTO c FROM sales_payment;
    ASSERT c = 2, 'expected 2 committed payments (10a + 10d); the blocked one must not persist. got '||c;
END$$;

-- 11. First-boot concurrency backstop: the partial unique index must make a
-- second auto trial impossible even if a caller bypasses licence_ensure_trial().
DO $$
DECLARE v_code text;
BEGIN
    SELECT install_code INTO v_code FROM licence_node WHERE id = 1;
    BEGIN
        INSERT INTO licence (mode, install_code, source, verified, starts_at, expires_at, trial_days)
        VALUES ('trial', v_code, 'auto', true, now(), now() + interval '14 days', 14);
        RAISE EXCEPTION 'SENTINEL: expected the unique index to reject a second auto trial';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%',
            'a second auto trial was allowed; licence_one_auto_trial_uidx is not protecting first boot';
    END;
END$$;

\echo '>>> ALL BEHAVIOUR ASSERTIONS PASSED'
