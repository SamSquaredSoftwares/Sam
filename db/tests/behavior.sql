-- ===========================================================================
-- behavior.sql — behavioural assertions for the SAMePOS licensing core.
-- Run against a database that has 024_licensing.sql applied AND a sales_order
-- stub/table present (so the hard-guard trigger is attached). Uses plpgsql
-- ASSERT; any failure aborts under psql -v ON_ERROR_STOP=1.
-- ===========================================================================
\set ON_ERROR_STOP on

-- Guard trigger must be attached (stub table existed at migration time).
DO $$
BEGIN
    ASSERT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_licence_guard_sales'),
        'hard-guard trigger was not attached to the sales table';
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

-- 10. Hard guard behaviour.
-- 10a. Valid licence -> sale allowed even with hard enforcement ON.
UPDATE licence_config SET hard_enforcement = true WHERE id = 1;
INSERT INTO sales_order (business_day_id) VALUES (1);

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

-- 10c. hard_enforcement ON + invalid -> sale BLOCKED.
DO $$
BEGIN
    BEGIN
        INSERT INTO sales_order (business_day_id) VALUES (2);
        RAISE EXCEPTION 'SENTINEL: expected the guard to block this sale';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'hard guard did NOT block a sale while invalid';
    END;
END$$;

-- 10d. Advisory only (hard_enforcement OFF) + invalid -> sale ALLOWED.
UPDATE licence_config SET hard_enforcement = false WHERE id = 1;
INSERT INTO sales_order (business_day_id) VALUES (3);
DO $$
DECLARE c int;
BEGIN
    SELECT count(*) INTO c FROM sales_order;
    ASSERT c = 2, 'expected 2 committed orders (10a + 10d); the blocked one must not persist. got '||c;
END$$;

\echo '>>> ALL BEHAVIOUR ASSERTIONS PASSED'
