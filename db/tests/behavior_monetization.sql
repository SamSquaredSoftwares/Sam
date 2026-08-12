-- ===========================================================================
-- behavior_monetization.sql — behavioural assertions for the SAMePOS
-- monetization core (usage billing, once-off charge, machine lock).
-- Run against a database that has 024_licensing.sql AND 025_monetization.sql
-- applied, AFTER behavior.sql (which sets the node identity to
-- INSTALL-AAA-0001). Uses plpgsql ASSERT; any failure aborts under
-- psql -v ON_ERROR_STOP=1.
-- ===========================================================================
\set ON_ERROR_STOP on

-- Binding / integrity triggers must be attached.
DO $$
DECLARE n int;
BEGIN
    ASSERT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_billing_binding_immutable'),
        'binding-immutable trigger missing on billing_machine_binding';
    ASSERT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_billing_lock_install_code'),
        'install-code lock trigger missing on licence_node';
    ASSERT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_billing_service_charge_guard'),
        'service-charge guard trigger missing';
    SELECT count(*) INTO n FROM pg_trigger WHERE tgname LIKE 'trg_billing%_no_truncate';
    ASSERT n = 5, 'expected 5 TRUNCATE guards (binding, batches, charges, invoices, audit), got '||n;
END$$;

-- 1. Clean slate: unbound, no once-off charge, nothing accrued.
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM billing_status;
    ASSERT s.install_code = 'INSTALL-AAA-0001', 'expected node from behavior.sql, got '||coalesce(s.install_code,'null');
    ASSERT NOT s.machine_bound,                 'must start unbound';
    ASSERT s.service_charge_status = 'none',    'expected no once-off charge, got '||s.service_charge_status;
    ASSERT s.receipt_row_fee_cents = 10,        'default per-row fee must be 10c, got '||s.receipt_row_fee_cents;
    ASSERT s.currency = 'ZAR',                  'default currency must be ZAR, got '||s.currency;
    ASSERT s.unbilled_rows = 0 AND s.unbilled_cents = 0 AND s.outstanding_cents = 0,
        'nothing should be accrued or outstanding yet';
    ASSERT billing_machine_ok('ANY-MACHINE'),   'unbound node must accept any machine';
END$$;

-- 2. Upload a receipt batch pre-binding: charged rows x 10c.
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-001', 137, 42, NULL,
                                                       '{"file":"receipts-001.csv"}'::jsonb);
    ASSERT r.success,                'first upload must succeed';
    ASSERT r.amount_cents = 1370,    '137 rows x 10c = 1370, got '||coalesce(r.amount_cents::text,'null');
    ASSERT r.currency = 'ZAR',       'charge currency must be ZAR';
END$$;

-- 3. Replaying the same batch_ref is an idempotent success — never billed twice.
DO $$
DECLARE r record; n int;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-001', 137);
    ASSERT r.success,                       'replay must succeed';
    ASSERT r.reason = 'already recorded',   'replay reason, got '||r.reason;
    ASSERT r.amount_cents = 1370,           'replay returns the original charge';
    SELECT count(*) INTO n FROM billing_receipt_batch WHERE batch_ref = 'B-001';
    ASSERT n = 1, 'replay must not create a second batch; found '||n;
    SELECT count(*) INTO n FROM billing_audit WHERE action = 'usage_charge';
    ASSERT n = 1, 'replay must not audit a second charge; found '||n;
END$$;

-- 4. Same batch_ref with a DIFFERENT row count is a conflict, not a charge.
DO $$
DECLARE r record; n int;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-001', 200);
    ASSERT NOT r.success,                       'conflicting replay must fail';
    ASSERT r.reason ILIKE '%conflict%',         'conflict reason, got '||r.reason;
    SELECT count(*) INTO n FROM billing_receipt_batch;
    ASSERT n = 1, 'conflict must not create a batch; found '||n;
END$$;

-- 5. Quote the once-off with the amount still to be confirmed (NULL).
DO $$
DECLARE q billing_service_charge; s record;
BEGIN
    q := billing_quote_service_charge(NULL, 'licensing + integration, amount TBC');
    ASSERT q.status = 'pending_confirmation', 'quote status, got '||q.status;
    ASSERT q.amount_cents IS NULL,            'TBC quote must have NULL amount';
    SELECT * INTO s FROM billing_status;
    ASSERT s.service_charge_status = 'pending_confirmation',
        'status must show pending quote, got '||s.service_charge_status;
END$$;

-- 6. Confirming while the amount is still TBC is rejected; nothing gets bound.
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_confirm_service_charge('MACH-001');
    ASSERT NOT r.success,                     'confirm without an amount must fail';
    ASSERT r.reason ILIKE '%amount not confirmed%', 'reason, got '||r.reason;
    ASSERT NOT EXISTS (SELECT 1 FROM billing_machine_binding),
        'a failed confirmation must not bind the machine';
END$$;

-- 6b. The client agrees the amount: re-quote the pending charge in place.
DO $$
DECLARE q billing_service_charge; n int;
BEGIN
    q := billing_quote_service_charge(250000, 'agreed with client');
    ASSERT q.amount_cents = 250000,           're-quote must set the agreed amount';
    ASSERT q.status = 'pending_confirmation', 're-quote must stay pending, got '||q.status;
    SELECT count(*) INTO n FROM billing_service_charge;
    ASSERT n = 1, 're-quote must not raise a second charge; found '||n;
END$$;

-- 7. The documented path: confirm WITHOUT re-passing the amount -> the quoted
--    amount is used, the charge is confirmed AND the machine is bound.
DO $$
DECLARE r record; b record; s record;
BEGIN
    SELECT * INTO r FROM billing_confirm_service_charge('MACH-001', NULL, 'sam', 'till-1');
    ASSERT r.success,                  'confirmation must succeed: '||r.reason;
    ASSERT r.amount_cents = 250000,    'confirmed amount, got '||coalesce(r.amount_cents::text,'null');
    SELECT * INTO b FROM billing_machine_binding WHERE id = 1;
    ASSERT b.machine_id = 'MACH-001',  'binding machine, got '||coalesce(b.machine_id,'null');
    ASSERT b.install_code = 'INSTALL-AAA-0001', 'binding install code';
    SELECT * INTO s FROM billing_status;
    ASSERT s.machine_bound,                       'status must show bound';
    ASSERT s.service_charge_status = 'confirmed', 'charge status, got '||s.service_charge_status;
    ASSERT s.service_charge_cents = 250000,       'charge amount in status';
    ASSERT billing_machine_ok('MACH-001'),        'bound machine must pass the boot check';
    ASSERT NOT billing_machine_ok('MACH-EVIL'),   'other machines must fail the boot check';
    ASSERT NOT billing_machine_ok(NULL),          'a missing machine id must fail once bound';
END$$;

-- 8. Re-confirming with the same machine is idempotent; a different amount is not.
DO $$
DECLARE r record; n int;
BEGIN
    SELECT * INTO r FROM billing_confirm_service_charge('MACH-001');
    ASSERT r.success AND r.reason = 'already confirmed',
        'same-machine reconfirm must be idempotent, got '||r.reason;
    SELECT * INTO r FROM billing_confirm_service_charge('MACH-001', 999999);
    ASSERT NOT r.success AND r.reason ILIKE '%amount conflict%',
        'silent re-pricing must be rejected, got '||r.reason;
    SELECT count(*) INTO n FROM billing_service_charge;
    ASSERT n = 1, 'still exactly one once-off charge; found '||n;
END$$;

-- 9. Confirming from a different machine is rejected (non-transferable).
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_confirm_service_charge('MACH-EVIL', 250000);
    ASSERT NOT r.success,                'foreign machine must be rejected';
    ASSERT r.reason ILIKE '%machine mismatch%', 'reason, got '||r.reason;
END$$;

-- 10. The binding row is immutable: no re-pointing, no cosmetic edits, no
--     deleting, and no TRUNCATE (row triggers do not fire on TRUNCATE).
DO $$
BEGIN
    BEGIN
        UPDATE billing_machine_binding SET machine_id = 'MACH-EVIL' WHERE id = 1;
        RAISE EXCEPTION 'SENTINEL: binding update should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'binding UPDATE was not blocked';
    END;
    BEGIN
        UPDATE billing_machine_binding SET hostname = 'somewhere-else' WHERE id = 1;
        RAISE EXCEPTION 'SENTINEL: cosmetic binding update should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%',
            'binding UPDATE of a non-identity column was not blocked (docs claim no updates at all)';
    END;
    BEGIN
        DELETE FROM billing_machine_binding WHERE id = 1;
        RAISE EXCEPTION 'SENTINEL: binding delete should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'binding DELETE was not blocked';
    END;
    BEGIN
        TRUNCATE billing_machine_binding;
        RAISE EXCEPTION 'SENTINEL: binding TRUNCATE should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%',
            'TRUNCATE erased the permanent machine binding (row triggers do not fire on TRUNCATE)';
    END;
    ASSERT (SELECT machine_id FROM billing_machine_binding WHERE id = 1) = 'MACH-001',
        'binding must be unchanged';
    ASSERT (SELECT hostname FROM billing_machine_binding WHERE id = 1) = 'till-1',
        'binding hostname must be unchanged';
END$$;

-- 11. The node's install code is locked once bound (no licence re-homing);
--     same-code updates (e.g. app version bumps) still work.
DO $$
BEGIN
    BEGIN
        PERFORM licence_set_node('DIFFERENT-CODE');
        RAISE EXCEPTION 'SENTINEL: install-code change should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'install-code change was not blocked';
    END;
    PERFORM licence_set_node('INSTALL-AAA-0001', '1.1.0');
    ASSERT (SELECT install_code FROM licence_node WHERE id = 1) = 'INSTALL-AAA-0001',
        'install code must be unchanged';
END$$;

-- 12. Once bound, uploads from the wrong machine (or with no machine id) are
--     rejected and audited.
DO $$
DECLARE r record; n int;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-BAD-1', 50, NULL, 'MACH-EVIL');
    ASSERT NOT r.success AND r.reason ILIKE '%machine mismatch%',
        'foreign-machine upload must be rejected, got '||r.reason;
    SELECT * INTO r FROM billing_record_receipt_upload('B-BAD-2', 50);
    ASSERT NOT r.success, 'upload without a machine id must be rejected once bound';
    SELECT count(*) INTO n FROM billing_receipt_batch;
    ASSERT n = 1, 'rejected uploads must not be charged; found '||n||' batches';
    SELECT count(*) INTO n FROM billing_audit WHERE action = 'usage_reject';
    ASSERT n >= 3, 'expected >=3 usage_reject audit rows, got '||n;
END$$;

-- 13. Uploads from the bound machine keep charging.
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-002', 63, 20, 'MACH-001');
    ASSERT r.success AND r.amount_cents = 630, '63 rows x 10c = 630';
END$$;

-- 14. Rate changes apply to NEW batches only; recorded charges never drift.
UPDATE billing_config SET receipt_row_fee_cents = 12, updated_at = now() WHERE id = 1;
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_record_receipt_upload('B-003', 100, NULL, 'MACH-001');
    ASSERT r.success AND r.amount_cents = 1200, '100 rows x 12c = 1200, got '||coalesce(r.amount_cents::text,'null');
    ASSERT (SELECT amount_cents FROM billing_receipt_batch WHERE batch_ref = 'B-001') = 1370,
        'old batch must keep its snapshotted 10c rate';
END$$;

-- 15. Accrual shows in the status readout.
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM billing_status;
    ASSERT s.unbilled_rows = 300,       '137+63+100 rows, got '||s.unbilled_rows;
    ASSERT s.unbilled_cents = 3200,     '1370+630+1200 cents, got '||s.unbilled_cents;
    ASSERT s.outstanding_cents = 0,     'nothing invoiced yet';
END$$;

-- 16. Invoice the period: usage + the once-off charge, single issued invoice.
DO $$
DECLARE r record; inv billing_invoice;
BEGIN
    SELECT * INTO r FROM billing_generate_invoice((now() - interval '1 day')::date,
                                                  (now() + interval '1 day')::date);
    ASSERT r.success,                'invoice generation must succeed: '||r.reason;
    ASSERT r.total_cents = 253200,   '3200 usage + 250000 once-off, got '||r.total_cents;
    SELECT * INTO inv FROM billing_invoice WHERE id = r.invoice_id;
    ASSERT inv.invoice_no LIKE 'INV-%', 'invoice number, got '||coalesce(inv.invoice_no,'null');
    ASSERT inv.usage_rows = 300 AND inv.usage_cents = 3200 AND inv.service_cents = 250000,
        'invoice line totals';
    ASSERT inv.status = 'issued',    'invoice must be issued';
    ASSERT NOT EXISTS (SELECT 1 FROM billing_receipt_batch WHERE invoice_id IS NULL),
        'every batch must now carry the invoice id';
    ASSERT (SELECT invoice_id FROM billing_service_charge LIMIT 1) = inv.id,
        'the once-off charge must carry the invoice id';
END$$;

-- 17. After invoicing: nothing unbilled, invoice total outstanding.
DO $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM billing_status;
    ASSERT s.unbilled_rows = 0 AND s.unbilled_cents = 0, 'accrual must reset after invoicing';
    ASSERT s.outstanding_cents = 253200, 'issued invoice must be outstanding, got '||s.outstanding_cents;
END$$;

-- 18. Re-invoicing the same period finds nothing (no double billing).
DO $$
DECLARE r record;
BEGIN
    SELECT * INTO r FROM billing_generate_invoice((now() - interval '1 day')::date,
                                                  (now() + interval '1 day')::date);
    ASSERT NOT r.success,                     're-invoicing must find nothing';
    ASSERT r.reason ILIKE '%nothing to invoice%', 'reason, got '||r.reason;
END$$;

-- 19. Settling the invoice settles the once-off charge and clears the balance.
DO $$
DECLARE inv billing_invoice; s record;
BEGIN
    inv := billing_mark_invoice_paid((SELECT id FROM billing_invoice ORDER BY id LIMIT 1), 'EFT-12345');
    ASSERT inv.status = 'paid' AND inv.paid_at IS NOT NULL, 'invoice must be paid';
    ASSERT (SELECT status FROM billing_service_charge LIMIT 1) = 'paid',
        'once-off charge must be paid with its invoice';
    SELECT * INTO s FROM billing_status;
    ASSERT s.outstanding_cents = 0,            'balance must clear';
    ASSERT s.service_charge_status = 'paid',   'status must show the once-off as paid';
END$$;

-- 20. The audit trail covers every money/binding event.
DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM billing_audit WHERE action = 'usage_charge';
    ASSERT n = 3, 'expected 3 usage_charge audits, got '||n;
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'service_quote'),   'quote must be audited';
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'service_confirm'), 'confirmation must be audited';
    SELECT count(*) INTO n FROM billing_audit WHERE action = 'service_reject';
    ASSERT n >= 3, 'expected >=3 service_reject audits (TBC amount, re-price, foreign machine), got '||n;
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'bind'),          'binding must be audited';
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'invoice_issue'), 'invoice issue must be audited';
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'invoice_paid'),  'invoice payment must be audited';
END$$;

-- 21. TRUNCATE cannot wipe any money table either (the usage ledger is the
--     revenue record; erasing it would erase the charges).
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['billing_receipt_batch', 'billing_service_charge',
                             'billing_invoice', 'billing_audit']
    LOOP
        BEGIN
            EXECUTE format('TRUNCATE %I', t);
            RAISE EXCEPTION 'SENTINEL: TRUNCATE of % should have been blocked', t;
        EXCEPTION WHEN others THEN
            ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'TRUNCATE was not blocked on '||t;
        END;
    END LOOP;
    ASSERT (SELECT count(*) FROM billing_receipt_batch) = 3, 'usage ledger must be intact';
END$$;

-- 22. An invoiced once-off charge cannot be cancelled out from under its
--     invoice (that would free the one-live-charge slot and allow a second).
DO $$
BEGIN
    BEGIN
        UPDATE billing_service_charge SET status = 'cancelled'
         WHERE invoice_id IS NOT NULL;
        RAISE EXCEPTION 'SENTINEL: cancelling an invoiced charge should have been blocked';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'cancelling an invoiced service charge was not blocked';
    END;
    ASSERT (SELECT status FROM billing_service_charge WHERE charge_type = 'licence_integration') = 'paid',
        'the once-off charge must still be paid';
END$$;

-- 23. Voiding an invoice RELEASES its usage back to unbilled instead of
--     stranding it; the released usage is then re-invoiceable.
DO $$
DECLARE r record; s record; v_inv bigint;
BEGIN
    PERFORM billing_record_receipt_upload('B-004', 40, 10, 'MACH-001');   -- 40 x 12c = 480
    SELECT * INTO r FROM billing_generate_invoice((now() - interval '1 day')::date,
                                                  (now() + interval '1 day')::date);
    ASSERT r.success AND r.total_cents = 480, 'second invoice should total 480, got '||coalesce(r.total_cents::text,'null');
    v_inv := r.invoice_id;

    PERFORM billing_void_invoice(v_inv, 'test: issued in error');
    ASSERT (SELECT status FROM billing_invoice WHERE id = v_inv) = 'void', 'invoice must be void';
    SELECT * INTO s FROM billing_status;
    ASSERT s.unbilled_cents = 480,
        'voiding must return the usage to unbilled, got '||s.unbilled_cents;
    ASSERT s.outstanding_cents = 0, 'a void invoice must not be outstanding, got '||s.outstanding_cents;

    SELECT * INTO r FROM billing_generate_invoice((now() - interval '1 day')::date,
                                                  (now() + interval '1 day')::date);
    ASSERT r.success AND r.total_cents = 480,
        'released usage must be re-invoiceable, got '||coalesce(r.reason,'null');
    PERFORM billing_mark_invoice_paid(r.invoice_id, 'EFT-67890');
    ASSERT EXISTS (SELECT 1 FROM billing_audit WHERE action = 'invoice_void'), 'void must be audited';
END$$;

-- 24. A paid invoice cannot be voided.
DO $$
BEGIN
    BEGIN
        PERFORM billing_void_invoice((SELECT id FROM billing_invoice WHERE status = 'paid' LIMIT 1));
        RAISE EXCEPTION 'SENTINEL: voiding a paid invoice should have been refused';
    EXCEPTION WHEN others THEN
        ASSERT SQLERRM NOT LIKE 'SENTINEL:%', 'voiding a paid invoice was not refused';
    END;
END$$;

-- 25. Usage left behind by a missed billing run is NOT stranded: the default
--     no-argument (previous calendar month) run sweeps every still-unbilled
--     batch before the cut-off, including ones older than that month.
DO $$
DECLARE r record; s record;
BEGIN
    PERFORM billing_record_receipt_upload('B-OLD-1', 25, 5, 'MACH-001');   -- 25 x 12c = 300
    PERFORM billing_record_receipt_upload('B-OLD-2', 15, 3, 'MACH-001');   -- 15 x 12c = 180
    -- pretend these were uploaded long before the last billing run
    UPDATE billing_receipt_batch SET uploaded_at = now() - interval '4 months'
     WHERE batch_ref = 'B-OLD-1';
    UPDATE billing_receipt_batch SET uploaded_at = now() - interval '20 days'
     WHERE batch_ref = 'B-OLD-2';

    SELECT * INTO r FROM billing_generate_invoice();   -- no arguments: the month-end job
    ASSERT r.success, 'default month-end run must invoice the stranded usage: '||coalesce(r.reason,'null');
    ASSERT r.total_cents = 480,
        'both old batches must be swept (300 + 180), got '||coalesce(r.total_cents::text,'null');
    SELECT * INTO s FROM billing_status;
    ASSERT s.unbilled_cents = 0, 'nothing may remain unbilled after the sweep, got '||s.unbilled_cents;
    PERFORM billing_mark_invoice_paid(r.invoice_id, 'EFT-OLD');
END$$;

-- 26. A confirmed but zero-amount (comped) charge still reaches an invoice and
--     can be settled — the invoicing guard keys on existence, not amount.
DO $$
DECLARE r record;
BEGIN
    INSERT INTO billing_service_charge (charge_type, install_code, amount_cents,
                                        currency, status, confirmed_at, confirmed_by)
    VALUES ('comped_extra', 'INSTALL-AAA-0001', 0, 'ZAR', 'confirmed', now(), 'test');
    SELECT * INTO r FROM billing_generate_invoice();
    ASSERT r.success, 'a zero-amount confirmed charge must still be invoiced: '||coalesce(r.reason,'null');
    ASSERT r.total_cents = 0, 'comped invoice total should be 0, got '||r.total_cents;
    ASSERT (SELECT invoice_id FROM billing_service_charge WHERE charge_type = 'comped_extra') = r.invoice_id,
        'the comped charge must be attached to the invoice';
    PERFORM billing_mark_invoice_paid(r.invoice_id, 'COMPED');
    ASSERT (SELECT status FROM billing_service_charge WHERE charge_type = 'comped_extra') = 'paid',
        'a comped charge must be able to reach paid';
END$$;

-- 27. Every invoice's stored totals match the batches actually stamped to it.
DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad
      FROM billing_invoice i
      LEFT JOIN (SELECT invoice_id, sum(row_count) rows, sum(amount_cents) cents
                   FROM billing_receipt_batch GROUP BY invoice_id) b
             ON b.invoice_id = i.id
     WHERE i.status <> 'void'
       AND (COALESCE(b.rows, 0) <> i.usage_rows OR COALESCE(b.cents, 0) <> i.usage_cents);
    ASSERT bad = 0, bad||' invoice(s) disagree with the batches stamped to them';
END$$;

\echo '>>> ALL MONETIZATION ASSERTIONS PASSED'
