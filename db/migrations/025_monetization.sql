-- ===========================================================================
-- 025_monetization.sql
-- SAMePOS monetization core: usage billing + once-off service charge + machine lock.
--
-- PostgreSQL (developed and tested against PG 16; requires >= 12 for generated
-- columns). Idempotent and ADDITIVE: safe to run on a fresh node or an existing
-- database. It never drops or alters existing product tables. Applied inside
-- one transaction; if anything fails the whole migration rolls back.
-- REQUIRES the licensing core (024_licensing.sql) to be applied first.
--
-- Revenue model (see docs/monetization.md):
--   * USAGE: every generated-receipts upload is metered and charged per
--     TRANSACTIONAL ROW at billing_config.receipt_row_fee_cents (default 10c).
--     billing_record_receipt_upload() is idempotent on batch_ref, snapshots
--     the rate, and audits every charge and rejection.
--   * ONCE-OFF: a licensing & integration service charge, quoted first (amount
--     may still be "to be confirmed") and then confirmed via
--     billing_confirm_service_charge().
--   * MACHINE LOCK: confirming the once-off charge permanently binds the
--     product to the machine id presented at confirmation. The binding row can
--     never be updated, deleted or TRUNCATEd, the node's install code is locked
--     from then on, and uploads from any other machine are rejected. Transfer
--     to another machine requires vendor intervention by design.
--   * Usage is rolled up (plus the once-off charge) into issued invoices by
--     billing_generate_invoice(); billing_status is the app-facing readout.
--
-- Money safety properties (all covered by db/tests/behavior_monetization.sql):
--   * no double-charging: batch_ref is the idempotency key, enforced by a
--     unique index rather than a read-then-write;
--   * no stranded usage: invoicing sweeps ALL still-unbilled batches before
--     period_end, so a missed billing run cannot leak a month of revenue;
--   * no stamped-but-unbilled rows: batches are stamped and totalled in one
--     UPDATE ... RETURNING statement;
--   * no concurrent double-issue or double-bind: confirmation and invoicing
--     serialise on a per-node advisory lock;
--   * voiding an invoice releases its usage back to unbilled instead of
--     stranding it (billing_void_invoice).
--
-- App integration points:
--   boot check       -> SELECT billing_machine_ok(:machine_id);
--   receipts upload  -> SELECT * FROM billing_record_receipt_upload(
--                           :batch_ref, :row_count, :receipt_count, :machine_id, :detail);
--   quote once-off   -> SELECT * FROM billing_quote_service_charge(:amount_cents, :notes);
--   confirm+bind     -> SELECT * FROM billing_confirm_service_charge(
--                           :machine_id, :amount_cents, :confirmed_by, :hostname);
--   /api/billing/status -> SELECT * FROM billing_status;
--   month-end        -> SELECT * FROM billing_generate_invoice();  -- previous month
--   settle           -> SELECT billing_mark_invoice_paid(:invoice_id, :reference);
--   correct a bill   -> SELECT billing_void_invoice(:invoice_id, :reason);
--
-- Run the month-end job with a stable session TimeZone (the node's trading
-- timezone): period boundaries are dates, so the TZ decides which month a
-- boundary upload falls in. Nothing is ever lost either way — an upload the
-- cut-off misses is swept by the next run.
-- ===========================================================================

BEGIN;

-- The monetization core builds on the licensing core's node identity
-- (licence_node.install_code). Refuse to apply without it.
DO $$
BEGIN
    IF to_regclass('public.licence_node') IS NULL THEN
        RAISE EXCEPTION 'monetization core requires the licensing core first: apply db/migrations/024_licensing.sql (or db/schema/licensing.schema.sql)';
    END IF;
END$$;

-- --- billing_config: singleton tuning row -----------------------------------
CREATE TABLE IF NOT EXISTS billing_config (
    id                    smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    currency              text        NOT NULL DEFAULT 'ZAR' CHECK (length(currency) = 3),
    receipt_row_fee_cents integer     NOT NULL DEFAULT 10 CHECK (receipt_row_fee_cents >= 0),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
INSERT INTO billing_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- --- billing_machine_binding: the permanent machine lock --------------------
-- Written once when the once-off service charge is confirmed. From then on the
-- product is bound to that machine id: the row can never be updated, deleted
-- or truncated away (triggers below).
CREATE TABLE IF NOT EXISTS billing_machine_binding (
    id           smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    install_code text        NOT NULL,
    machine_id   text        NOT NULL,   -- hardware fingerprint computed by the app
    hostname     text,
    bound_by     text,
    bound_at     timestamptz NOT NULL DEFAULT now()
);

-- --- billing_invoice: issued bills (usage roll-up + once-off charge) --------
CREATE TABLE IF NOT EXISTS billing_invoice (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_no     text        UNIQUE,
    install_code   text        NOT NULL,
    period_start   date        NOT NULL,   -- nominal label only (see billing_generate_invoice)
    period_end     date        NOT NULL,   -- exclusive sweep boundary
    usage_rows     bigint      NOT NULL DEFAULT 0 CHECK (usage_rows >= 0),
    usage_cents    bigint      NOT NULL DEFAULT 0 CHECK (usage_cents >= 0),
    service_cents  bigint      NOT NULL DEFAULT 0 CHECK (service_cents >= 0),
    total_cents    bigint      NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    currency       text        NOT NULL,
    status         text        NOT NULL DEFAULT 'issued'
                                CHECK (status IN ('issued', 'paid', 'void')),
    issued_at      timestamptz NOT NULL DEFAULT now(),
    paid_at        timestamptz,
    paid_reference text,
    voided_at      timestamptz,
    voided_reason  text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_invoice_period CHECK (period_end >= period_start)
);
-- Older builds created the table without the void columns; add them in place.
ALTER TABLE billing_invoice ADD COLUMN IF NOT EXISTS voided_at    timestamptz;
ALTER TABLE billing_invoice ADD COLUMN IF NOT EXISTS voided_reason text;

-- --- billing_service_charge: the once-off licensing & integration fee -------
-- amount_cents may be NULL while the quote is still "to be confirmed"; it must
-- be set before (or at) confirmation.
CREATE TABLE IF NOT EXISTS billing_service_charge (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    charge_type  text        NOT NULL DEFAULT 'licence_integration',
    install_code text        NOT NULL,
    amount_cents bigint      CHECK (amount_cents IS NULL OR amount_cents >= 0),
    currency     text        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending_confirmation'
                              CHECK (status IN ('pending_confirmation', 'confirmed',
                                                'paid', 'cancelled')),
    invoice_id   bigint      REFERENCES billing_invoice(id),
    quoted_at    timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    confirmed_by text,
    paid_at      timestamptz,
    notes        text,
    CONSTRAINT billing_service_amount_confirmed
        CHECK (status NOT IN ('confirmed', 'paid') OR amount_cents IS NOT NULL)
);
-- at most one live once-off charge per node
CREATE UNIQUE INDEX IF NOT EXISTS billing_service_charge_live_uidx
    ON billing_service_charge (install_code, charge_type)
    WHERE status <> 'cancelled';

-- --- billing_receipt_batch: one row per uploaded receipt batch --------------
-- The usage-metering ledger. batch_ref is the caller's idempotency key: the
-- same upload replayed can never be charged twice. rate_cents is snapshotted
-- at charge time so later price changes never rewrite history; amount_cents is
-- a generated column, so a stored charge can never drift from rows x rate.
CREATE TABLE IF NOT EXISTS billing_receipt_batch (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    install_code  text        NOT NULL,
    batch_ref     text        NOT NULL UNIQUE,
    receipt_count integer     NOT NULL DEFAULT 0 CHECK (receipt_count >= 0),
    row_count     integer     NOT NULL CHECK (row_count > 0),
    rate_cents    integer     NOT NULL CHECK (rate_cents >= 0),
    amount_cents  bigint      GENERATED ALWAYS AS ((row_count::bigint) * (rate_cents::bigint)) STORED,
    currency      text        NOT NULL,
    machine_id    text,
    invoice_id    bigint      REFERENCES billing_invoice(id),
    uploaded_at   timestamptz NOT NULL DEFAULT now(),
    detail        jsonb
);
CREATE INDEX IF NOT EXISTS billing_receipt_batch_unbilled_idx
    ON billing_receipt_batch (uploaded_at) WHERE invoice_id IS NULL;

-- --- billing_audit: append-only money/binding trail -------------------------
CREATE TABLE IF NOT EXISTS billing_audit (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    action       text        NOT NULL,   -- usage_charge|usage_reject|service_quote|service_confirm|service_reject|bind|invoice_issue|invoice_paid|invoice_void
    install_code text,
    ref_id       bigint,
    outcome      text        NOT NULL DEFAULT 'ok',   -- ok|rejected|error
    detail       jsonb
);

-- ===========================================================================
-- Enforcement triggers: machine binding, TRUNCATE, charge state
-- ===========================================================================

-- billing_machine_binding is write-once: never updated, never deleted. This is
-- what makes the product non-transferable after activation.
CREATE OR REPLACE FUNCTION billing_binding_immutable()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'machine binding is permanent: this product is bound to machine % and cannot be altered, unbound or transferred',
        OLD.machine_id
        USING ERRCODE = 'check_violation';
END$$;

DROP TRIGGER IF EXISTS trg_billing_binding_immutable ON billing_machine_binding;
CREATE TRIGGER trg_billing_binding_immutable
    BEFORE UPDATE OR DELETE ON billing_machine_binding
    FOR EACH ROW EXECUTE FUNCTION billing_binding_immutable();

-- Row-level triggers do NOT fire on TRUNCATE, so an UPDATE/DELETE guard alone
-- would leave `TRUNCATE billing_machine_binding` as a one-statement way to drop
-- the machine lock (and the same trick would erase the usage ledger). Statement
-- triggers close that hole on every table that holds money or the binding.
CREATE OR REPLACE FUNCTION billing_no_truncate()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'TRUNCATE is not permitted on %: it holds the permanent machine binding / billing record',
        TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
END$$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['billing_machine_binding', 'billing_receipt_batch',
                             'billing_service_charge', 'billing_invoice', 'billing_audit']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', t, t);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION billing_no_truncate()', t, t);
    END LOOP;
END$$;

-- Once bound, the node's install code is locked too: licence_set_node() can no
-- longer re-home the node to a different install code (which would otherwise
-- be a licence-transfer loophole). BEFORE INSERT as well as UPDATE, so deleting
-- the identity row and re-inserting a different code is blocked too.
CREATE OR REPLACE FUNCTION billing_lock_install_code()
RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_bound text;
BEGIN
    SELECT b.install_code INTO v_bound FROM billing_machine_binding b WHERE b.id = 1;
    IF v_bound IS NOT NULL AND NEW.install_code IS DISTINCT FROM v_bound THEN
        RAISE EXCEPTION
            'install code is locked to % by the machine binding; this product is non-transferable',
            v_bound
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END$$;

DROP TRIGGER IF EXISTS trg_billing_lock_install_code ON licence_node;
CREATE TRIGGER trg_billing_lock_install_code
    BEFORE INSERT OR UPDATE ON licence_node
    FOR EACH ROW EXECUTE FUNCTION billing_lock_install_code();

-- An invoiced once-off charge cannot be cancelled out from under its invoice:
-- cancelling frees the one-live-charge slot, so allowing it while the charge
-- sits on an issued/paid invoice would let a second once-off charge be raised
-- for the same node. Void the invoice first (billing_void_invoice) — that
-- detaches the charge and then cancelling is allowed.
CREATE OR REPLACE FUNCTION billing_service_charge_guard()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'cancelled' AND OLD.status <> 'cancelled'
       AND OLD.invoice_id IS NOT NULL THEN
        RAISE EXCEPTION
            'service charge % is attached to invoice % and cannot be cancelled; void the invoice first',
            OLD.id, OLD.invoice_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END$$;

DROP TRIGGER IF EXISTS trg_billing_service_charge_guard ON billing_service_charge;
CREATE TRIGGER trg_billing_service_charge_guard
    BEFORE UPDATE ON billing_service_charge
    FOR EACH ROW EXECUTE FUNCTION billing_service_charge_guard();

-- ===========================================================================
-- Functions
-- ===========================================================================

-- Per-node advisory lock key. Serialises confirmation/binding and invoice
-- generation so concurrent callers cannot both "win" a check-then-act.
CREATE OR REPLACE FUNCTION billing_lock_key(p_install_code text)
RETURNS void
LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(hashtext('samepos.billing'),
                                 hashtext(COALESCE(p_install_code, '')));
$$;

-- True when the product is either not yet bound (trial / pre-activation) or
-- bound to exactly this machine. The app should call this on boot and refuse
-- to run when false.
CREATE OR REPLACE FUNCTION billing_machine_ok(p_machine_id text)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        (SELECT b.machine_id IS NOT DISTINCT FROM btrim(p_machine_id)
           FROM billing_machine_binding b WHERE b.id = 1),
        true);
$$;

-- Meter a generated-receipts upload: one batch == one upload, charged at the
-- configured fee per transactional row (rate snapshotted onto the batch).
--
-- Business rejections (machine mismatch, batch_ref conflict) return
-- success=false with a reason and are audited, following the licensing core's
-- pattern (a RAISE would roll back the reject audit row). Replaying an
-- already-recorded batch_ref with the same row count is an idempotent SUCCESS
-- that returns the existing charge and never bills twice — including when two
-- replays race, which the ON CONFLICT DO NOTHING insert resolves without
-- raising. Programming errors (no node identity, empty batch_ref, non-positive
-- row_count) still raise.
CREATE OR REPLACE FUNCTION billing_record_receipt_upload(
    p_batch_ref     text,
    p_row_count     integer,
    p_receipt_count integer DEFAULT NULL,
    p_machine_id    text    DEFAULT NULL,
    p_detail        jsonb   DEFAULT NULL
) RETURNS TABLE (success boolean, reason text, batch_id bigint,
                 amount_cents bigint, currency text)
LANGUAGE plpgsql AS $$
DECLARE
    v_node     licence_node;
    v_cfg      billing_config;
    v_bind     billing_machine_binding;
    v_ref      text;
    v_existing billing_receipt_batch;
    v_batch    billing_receipt_batch;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;
    IF p_batch_ref IS NULL OR length(btrim(p_batch_ref)) = 0 THEN
        RAISE EXCEPTION 'batch_ref must be a non-empty string';
    END IF;
    IF p_row_count IS NULL OR p_row_count <= 0 THEN
        RAISE EXCEPTION 'row_count must be a positive integer';
    END IF;
    v_ref := btrim(p_batch_ref);

    SELECT * INTO v_cfg  FROM billing_config WHERE id = 1;
    SELECT * INTO v_bind FROM billing_machine_binding WHERE id = 1;

    -- Once bound, uploads are only accepted from the bound machine.
    IF v_bind.machine_id IS NOT NULL
       AND v_bind.machine_id IS DISTINCT FROM btrim(p_machine_id) THEN
        INSERT INTO billing_audit (action, install_code, outcome, detail)
        VALUES ('usage_reject', v_node.install_code, 'rejected',
                jsonb_build_object('reason', 'machine mismatch',
                                   'batch_ref', v_ref,
                                   'presented_machine_id', p_machine_id));
        RETURN QUERY SELECT false,
            'machine mismatch: this product is bound to a different machine'::text,
            NULL::bigint, NULL::bigint, v_cfg.currency;
        RETURN;
    END IF;

    -- Insert-or-nothing: the unique batch_ref is the idempotency key, and
    -- letting the index arbitrate means concurrent replays cannot both insert.
    INSERT INTO billing_receipt_batch
        (install_code, batch_ref, receipt_count, row_count, rate_cents,
         currency, machine_id, detail)
    VALUES (v_node.install_code, v_ref, COALESCE(p_receipt_count, 0),
            p_row_count, v_cfg.receipt_row_fee_cents,
            v_cfg.currency, btrim(p_machine_id), p_detail)
    ON CONFLICT (batch_ref) DO NOTHING
    RETURNING * INTO v_batch;

    -- Nothing inserted => this batch_ref was already recorded (by an earlier
    -- call or a concurrent one). Replays match; a different row count conflicts.
    IF v_batch.id IS NULL THEN
        SELECT * INTO v_existing FROM billing_receipt_batch b WHERE b.batch_ref = v_ref;
        IF v_existing.row_count = p_row_count THEN
            RETURN QUERY SELECT true, 'already recorded'::text,
                v_existing.id, v_existing.amount_cents, v_existing.currency;
        ELSE
            INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
            VALUES ('usage_reject', v_node.install_code, v_existing.id, 'rejected',
                    jsonb_build_object('reason', 'batch_ref conflict',
                                       'batch_ref', v_ref,
                                       'stored_rows', v_existing.row_count,
                                       'presented_rows', p_row_count));
            RETURN QUERY SELECT false,
                format('batch_ref conflict: %s already recorded with %s rows',
                       v_ref, v_existing.row_count),
                v_existing.id, NULL::bigint, v_existing.currency;
        END IF;
        RETURN;
    END IF;

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('usage_charge', v_node.install_code, v_batch.id, 'ok',
            jsonb_build_object('batch_ref', v_batch.batch_ref,
                               'rows', v_batch.row_count,
                               'rate_cents', v_batch.rate_cents,
                               'amount_cents', v_batch.amount_cents));

    RETURN QUERY SELECT true, 'charged'::text, v_batch.id,
                        v_batch.amount_cents, v_batch.currency;
END$$;

-- Quote (or re-quote while still pending) the once-off licensing & integration
-- service charge. amount_cents NULL = amount still to be confirmed with the
-- client. Idempotent: an existing live quote is returned (and only a PENDING
-- one is re-priced).
CREATE OR REPLACE FUNCTION billing_quote_service_charge(
    p_amount_cents bigint DEFAULT NULL,
    p_notes        text   DEFAULT NULL
) RETURNS billing_service_charge
LANGUAGE plpgsql AS $$
DECLARE
    v_node licence_node;
    v_cfg  billing_config;
    v_chg  billing_service_charge;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;
    IF p_amount_cents IS NOT NULL AND p_amount_cents < 0 THEN
        RAISE EXCEPTION 'amount_cents must be >= 0';
    END IF;
    PERFORM billing_lock_key(v_node.install_code);
    SELECT * INTO v_cfg FROM billing_config WHERE id = 1;

    SELECT * INTO v_chg FROM billing_service_charge c
     WHERE c.install_code = v_node.install_code
       AND c.charge_type = 'licence_integration'
       AND c.status <> 'cancelled'
     ORDER BY c.id DESC LIMIT 1;

    IF FOUND THEN
        IF v_chg.status = 'pending_confirmation' AND p_amount_cents IS NOT NULL THEN
            UPDATE billing_service_charge
               SET amount_cents = p_amount_cents,
                   notes        = COALESCE(p_notes, notes),
                   quoted_at    = now()
             WHERE id = v_chg.id
            RETURNING * INTO v_chg;
            INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
            VALUES ('service_quote', v_node.install_code, v_chg.id, 'ok',
                    jsonb_build_object('amount_cents', p_amount_cents,
                                       'requote', true));
        END IF;
        RETURN v_chg;   -- confirmed/paid charges are returned unchanged
    END IF;

    INSERT INTO billing_service_charge
        (charge_type, install_code, amount_cents, currency, notes)
    VALUES ('licence_integration', v_node.install_code, p_amount_cents,
            v_cfg.currency, p_notes)
    RETURNING * INTO v_chg;

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('service_quote', v_node.install_code, v_chg.id, 'ok',
            jsonb_build_object('amount_cents', p_amount_cents));

    RETURN v_chg;
END$$;

-- Confirm the once-off charge and PERMANENTLY bind the product to the machine.
-- The amount must be known by now: either already on the quote, or passed here.
-- On success the machine binding is written (if not already present) and can
-- never be changed again. Re-confirming with the same machine is an idempotent
-- success; a different machine, or a different amount than the confirmed one,
-- is rejected (success=false) and audited.
--
-- The whole check-then-bind runs under the node's advisory lock, so two
-- confirmations presenting different machine ids cannot both pass the mismatch
-- check: the loser waits, re-reads the now-committed binding, and is rejected.
CREATE OR REPLACE FUNCTION billing_confirm_service_charge(
    p_machine_id   text,
    p_amount_cents bigint DEFAULT NULL,
    p_confirmed_by text   DEFAULT NULL,
    p_hostname     text   DEFAULT NULL
) RETURNS TABLE (success boolean, reason text, charge_id bigint, amount_cents bigint)
LANGUAGE plpgsql AS $$
DECLARE
    v_node    licence_node;
    v_cfg     billing_config;
    v_bind    billing_machine_binding;
    v_chg     billing_service_charge;
    v_has     boolean;
    v_amount  bigint;
    v_machine text;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;
    IF p_machine_id IS NULL OR length(btrim(p_machine_id)) = 0 THEN
        RAISE EXCEPTION 'machine_id must be a non-empty string';
    END IF;
    IF p_amount_cents IS NOT NULL AND p_amount_cents < 0 THEN
        RAISE EXCEPTION 'amount_cents must be >= 0';
    END IF;
    v_machine := btrim(p_machine_id);

    PERFORM billing_lock_key(v_node.install_code);

    SELECT * INTO v_cfg  FROM billing_config WHERE id = 1;
    SELECT * INTO v_bind FROM billing_machine_binding WHERE id = 1;

    -- The product can only ever be bound to one machine.
    IF v_bind.machine_id IS NOT NULL
       AND v_bind.machine_id IS DISTINCT FROM v_machine THEN
        INSERT INTO billing_audit (action, install_code, outcome, detail)
        VALUES ('service_reject', v_node.install_code, 'rejected',
                jsonb_build_object('reason', 'machine mismatch',
                                   'bound_machine_id', v_bind.machine_id,
                                   'presented_machine_id', v_machine));
        RETURN QUERY SELECT false,
            'machine mismatch: product already bound to a different machine (non-transferable)'::text,
            NULL::bigint, NULL::bigint;
        RETURN;
    END IF;

    SELECT * INTO v_chg FROM billing_service_charge c
     WHERE c.install_code = v_node.install_code
       AND c.charge_type = 'licence_integration'
       AND c.status <> 'cancelled'
     ORDER BY c.id DESC LIMIT 1;
    v_has := FOUND;

    -- Already confirmed: idempotent success, but never a silent re-price.
    IF v_has AND v_chg.status IN ('confirmed', 'paid') THEN
        IF p_amount_cents IS NOT NULL
           AND p_amount_cents IS DISTINCT FROM v_chg.amount_cents THEN
            INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
            VALUES ('service_reject', v_node.install_code, v_chg.id, 'rejected',
                    jsonb_build_object('reason', 'amount conflict',
                                       'confirmed_cents', v_chg.amount_cents,
                                       'presented_cents', p_amount_cents));
            RETURN QUERY SELECT false,
                format('amount conflict: charge already %s at %s cents',
                       v_chg.status, v_chg.amount_cents),
                v_chg.id, v_chg.amount_cents;
            RETURN;
        END IF;
        IF v_bind.machine_id IS NULL THEN
            PERFORM billing_bind_machine(v_node.install_code, v_machine,
                                         p_hostname, p_confirmed_by, v_chg.id);
        END IF;
        RETURN QUERY SELECT true, 'already confirmed'::text,
                            v_chg.id, v_chg.amount_cents;
        RETURN;
    END IF;

    v_amount := COALESCE(p_amount_cents, v_chg.amount_cents);
    IF v_amount IS NULL THEN
        INSERT INTO billing_audit (action, install_code, outcome, detail)
        VALUES ('service_reject', v_node.install_code, 'rejected',
                jsonb_build_object('reason', 'amount not confirmed'));
        RETURN QUERY SELECT false,
            'amount not confirmed: quote the once-off amount (or pass amount_cents) before confirming'::text,
            NULL::bigint, NULL::bigint;
        RETURN;
    END IF;

    IF v_has THEN
        UPDATE billing_service_charge
           SET amount_cents = v_amount,
               status       = 'confirmed',
               confirmed_at = now(),
               confirmed_by = p_confirmed_by
         WHERE id = v_chg.id
        RETURNING * INTO v_chg;
    ELSE
        INSERT INTO billing_service_charge
            (charge_type, install_code, amount_cents, currency,
             status, confirmed_at, confirmed_by)
        VALUES ('licence_integration', v_node.install_code, v_amount,
                v_cfg.currency, 'confirmed', now(), p_confirmed_by)
        RETURNING * INTO v_chg;
    END IF;

    -- Bind the product to this machine — permanent from here on.
    IF v_bind.machine_id IS NULL THEN
        PERFORM billing_bind_machine(v_node.install_code, v_machine,
                                     p_hostname, p_confirmed_by, v_chg.id);
    END IF;

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('service_confirm', v_node.install_code, v_chg.id, 'ok',
            jsonb_build_object('amount_cents', v_amount,
                               'machine_id', v_machine));

    RETURN QUERY SELECT true, 'confirmed'::text, v_chg.id, v_chg.amount_cents;
END$$;

-- Write the permanent binding. Callers hold the node's advisory lock, so the
-- row cannot already exist for a different machine; a plain INSERT (not
-- ON CONFLICT DO NOTHING) means a lost race would raise rather than silently
-- audit a bind that did not happen.
CREATE OR REPLACE FUNCTION billing_bind_machine(
    p_install_code text,
    p_machine_id   text,
    p_hostname     text DEFAULT NULL,
    p_bound_by     text DEFAULT NULL,
    p_charge_id    bigint DEFAULT NULL
) RETURNS billing_machine_binding
LANGUAGE plpgsql AS $$
DECLARE v_bind billing_machine_binding;
BEGIN
    INSERT INTO billing_machine_binding (id, install_code, machine_id, hostname, bound_by)
    VALUES (1, p_install_code, p_machine_id, p_hostname, p_bound_by)
    RETURNING * INTO v_bind;

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('bind', p_install_code, p_charge_id, 'ok',
            jsonb_build_object('machine_id', p_machine_id, 'hostname', p_hostname));
    RETURN v_bind;
END$$;

-- Roll unbilled usage plus any confirmed, not-yet-invoiced once-off charge into
-- one issued invoice.
--
-- The sweep is deliberately "everything still unbilled with uploaded_at <
-- period_end", NOT a two-sided window: a lower bound would permanently strand
-- any batch that a missed billing run left behind (a node powered off over a
-- month end would leak that month's usage forever). period_start is therefore
-- the invoice's nominal label; period_end is the real cut-off.
--
-- Batches are stamped and totalled in ONE statement (UPDATE ... RETURNING), so
-- an upload committing mid-run is either fully counted or not stamped at all —
-- never stamped-but-unbilled. The whole run holds the node's advisory lock, so
-- two concurrent runs cannot both issue an invoice for the same usage.
-- 'Nothing to invoice' is a business result (success=false), not an error.
CREATE OR REPLACE FUNCTION billing_generate_invoice(
    p_period_start date DEFAULT NULL,
    p_period_end   date DEFAULT NULL
) RETURNS TABLE (success boolean, reason text, invoice_id bigint, total_cents bigint)
LANGUAGE plpgsql AS $$
DECLARE
    v_node     licence_node;
    v_cfg      billing_config;
    v_start    date;
    v_end      date;
    v_rows     bigint := 0;
    v_usage    bigint := 0;
    v_ccount   integer := 0;
    v_bcur     text;
    v_chg      billing_service_charge;
    v_service  bigint := 0;
    v_scur     text;
    v_took_chg boolean := false;
    v_currency text;
    v_inv      billing_invoice;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;

    v_start := COALESCE(p_period_start, (date_trunc('month', now() - interval '1 month'))::date);
    v_end   := COALESCE(p_period_end,   (date_trunc('month', now()))::date);
    IF v_end < v_start THEN
        RAISE EXCEPTION 'period_end must be >= period_start';
    END IF;

    PERFORM billing_lock_key(v_node.install_code);
    SELECT * INTO v_cfg FROM billing_config WHERE id = 1;

    SELECT * INTO v_chg FROM billing_service_charge c
     WHERE c.install_code = v_node.install_code
       AND c.status = 'confirmed'
       AND c.invoice_id IS NULL
     ORDER BY c.id LIMIT 1;

    -- Nothing billable at all? Say so before creating an invoice shell. A
    -- zero-amount (comped) once-off charge still counts as billable so that it
    -- gets attached to an invoice and can reach 'paid'.
    IF v_chg.id IS NULL
       AND NOT EXISTS (SELECT 1 FROM billing_receipt_batch b
                        WHERE b.invoice_id IS NULL AND b.uploaded_at < v_end) THEN
        RETURN QUERY SELECT false, 'nothing to invoice for this period'::text,
                            NULL::bigint, NULL::bigint;
        RETURN;
    END IF;

    INSERT INTO billing_invoice
        (install_code, period_start, period_end, currency)
    VALUES (v_node.install_code, v_start, v_end, v_cfg.currency)
    RETURNING * INTO v_inv;

    -- Stamp and total in one statement: no snapshot gap between them.
    WITH stamped AS (
        UPDATE billing_receipt_batch b
           SET invoice_id = v_inv.id
         WHERE b.invoice_id IS NULL
           AND b.uploaded_at < v_end
        RETURNING b.row_count, b.amount_cents, b.currency
    )
    SELECT COALESCE(sum(s.row_count), 0), COALESCE(sum(s.amount_cents), 0),
           count(DISTINCT s.currency), min(s.currency)
      INTO v_rows, v_usage, v_ccount, v_bcur
      FROM stamped s;

    IF v_chg.id IS NOT NULL THEN
        UPDATE billing_service_charge c
           SET invoice_id = v_inv.id
         WHERE c.id = v_chg.id AND c.invoice_id IS NULL
        RETURNING c.amount_cents, c.currency INTO v_service, v_scur;
        v_took_chg := FOUND;
        IF NOT v_took_chg THEN
            v_service := 0; v_scur := NULL;
        END IF;
    END IF;

    -- Money of different currencies must never be summed into one total.
    IF v_ccount > 1 OR (v_bcur IS NOT NULL AND v_scur IS NOT NULL AND v_bcur <> v_scur) THEN
        RAISE EXCEPTION
            'cannot invoice mixed currencies (usage %, service %); settle the outstanding items before changing billing_config.currency',
            COALESCE(v_bcur, '-'), COALESCE(v_scur, '-');
    END IF;
    -- Bill in the currency the charges were recorded in, not today's config.
    v_currency := COALESCE(v_bcur, v_scur, v_cfg.currency);

    IF v_rows = 0 AND NOT v_took_chg THEN
        -- Everything was swept by a concurrent run between the pre-check and
        -- the stamp; drop the empty shell rather than issue a zero invoice.
        DELETE FROM billing_invoice WHERE id = v_inv.id;
        RETURN QUERY SELECT false, 'nothing to invoice for this period'::text,
                            NULL::bigint, NULL::bigint;
        RETURN;
    END IF;

    UPDATE billing_invoice
       SET usage_rows    = v_rows,
           usage_cents   = v_usage,
           service_cents = v_service,
           total_cents   = v_usage + v_service,
           currency      = v_currency,
           invoice_no    = format('INV-%s-%s', to_char(v_start, 'YYYYMM'),
                                  lpad(v_inv.id::text, 6, '0'))
     WHERE id = v_inv.id
    RETURNING * INTO v_inv;

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('invoice_issue', v_node.install_code, v_inv.id, 'ok',
            jsonb_build_object('invoice_no', v_inv.invoice_no,
                               'usage_rows', v_rows,
                               'usage_cents', v_usage,
                               'service_cents', v_service,
                               'total_cents', v_inv.total_cents));

    RETURN QUERY SELECT true, 'issued'::text, v_inv.id, v_inv.total_cents;
END$$;

-- Settle an issued invoice; any once-off charge on it becomes paid too.
CREATE OR REPLACE FUNCTION billing_mark_invoice_paid(
    p_invoice_id bigint,
    p_reference  text DEFAULT NULL
) RETURNS billing_invoice
LANGUAGE plpgsql AS $$
DECLARE v_inv billing_invoice;
BEGIN
    UPDATE billing_invoice
       SET status = 'paid', paid_at = now(), paid_reference = p_reference
     WHERE id = p_invoice_id AND status = 'issued'
    RETURNING * INTO v_inv;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invoice % not found or not in issued state', p_invoice_id;
    END IF;

    UPDATE billing_service_charge
       SET status = 'paid', paid_at = now()
     WHERE invoice_id = v_inv.id AND status = 'confirmed';

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('invoice_paid', v_inv.install_code, v_inv.id, 'ok',
            jsonb_build_object('reference', p_reference,
                               'total_cents', v_inv.total_cents));
    RETURN v_inv;
END$$;

-- Void an issued invoice and RELEASE everything on it: batches and the once-off
-- charge go back to unbilled so the next run re-bills them. Without this,
-- voiding an invoice by hand would strand its usage forever (invoice_id set,
-- so never re-selected) — the money would vanish from every readout.
CREATE OR REPLACE FUNCTION billing_void_invoice(
    p_invoice_id bigint,
    p_reason     text DEFAULT NULL
) RETURNS billing_invoice
LANGUAGE plpgsql AS $$
DECLARE
    v_inv   billing_invoice;
    v_rows  bigint;
BEGIN
    UPDATE billing_invoice
       SET status = 'void', voided_at = now(), voided_reason = p_reason
     WHERE id = p_invoice_id AND status = 'issued'
    RETURNING * INTO v_inv;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invoice % not found or not in issued state (paid invoices cannot be voided)',
                        p_invoice_id;
    END IF;

    UPDATE billing_receipt_batch SET invoice_id = NULL WHERE invoice_id = v_inv.id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    UPDATE billing_service_charge SET invoice_id = NULL
     WHERE invoice_id = v_inv.id AND status = 'confirmed';

    INSERT INTO billing_audit (action, install_code, ref_id, outcome, detail)
    VALUES ('invoice_void', v_inv.install_code, v_inv.id, 'ok',
            jsonb_build_object('reason', p_reason,
                               'released_batches', v_rows,
                               'total_cents', v_inv.total_cents));
    RETURN v_inv;
END$$;

-- The core status readout: binding, once-off charge, current rate, accrued
-- (unbilled) usage and outstanding invoiced balance. The shape
-- /api/billing/status should return.
CREATE OR REPLACE FUNCTION billing_effective_status()
RETURNS TABLE (
    install_code          text,
    machine_bound         boolean,
    machine_id            text,
    bound_at              timestamptz,
    service_charge_status text,     -- none|pending_confirmation|confirmed|paid
    service_charge_cents  bigint,
    receipt_row_fee_cents integer,
    currency              text,
    unbilled_rows         bigint,
    unbilled_cents        bigint,
    outstanding_cents     bigint,   -- issued, unpaid invoices
    checked_at            timestamptz
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_node   licence_node;
    v_cfg    billing_config;
    v_bind   billing_machine_binding;
    v_chg    billing_service_charge;
    v_urows  bigint;
    v_ucents bigint;
    v_out    bigint;
    v_now    timestamptz := now();
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    SELECT * INTO v_cfg  FROM billing_config WHERE id = 1;
    SELECT * INTO v_bind FROM billing_machine_binding WHERE id = 1;

    SELECT * INTO v_chg FROM billing_service_charge c
     WHERE c.install_code = v_node.install_code
       AND c.charge_type = 'licence_integration'
       AND c.status <> 'cancelled'
     ORDER BY c.id DESC LIMIT 1;

    SELECT COALESCE(sum(b.row_count), 0), COALESCE(sum(b.amount_cents), 0)
      INTO v_urows, v_ucents
      FROM billing_receipt_batch b
     WHERE b.invoice_id IS NULL;

    SELECT COALESCE(sum(i.total_cents), 0) INTO v_out
      FROM billing_invoice i
     WHERE i.status = 'issued';

    RETURN QUERY SELECT
        v_node.install_code,
        v_bind.machine_id IS NOT NULL,
        v_bind.machine_id,
        v_bind.bound_at,
        COALESCE(v_chg.status, 'none'),
        v_chg.amount_cents,
        v_cfg.receipt_row_fee_cents,
        v_cfg.currency,
        v_urows,
        v_ucents,
        v_out,
        v_now;
END$$;

-- Convenience view (what /api/billing/status should return).
CREATE OR REPLACE VIEW billing_status AS SELECT * FROM billing_effective_status();

COMMENT ON TABLE  billing_config          IS 'SAMePOS monetization tuning: currency + per-transactional-row receipt fee. Monetization core v1.';
COMMENT ON TABLE  billing_machine_binding IS 'Permanent machine lock written when the once-off service charge is confirmed; non-transferable (no update/delete/truncate).';
COMMENT ON TABLE  billing_receipt_batch   IS 'Usage-metering ledger: one row per generated-receipts upload, charged per transactional row at the snapshotted rate.';
COMMENT ON TABLE  billing_service_charge  IS 'Once-off licensing & integration service charge (amount may be pending confirmation).';
COMMENT ON TABLE  billing_invoice         IS 'Issued bills: usage roll-up plus the once-off charge.';
COMMENT ON VIEW   billing_status          IS 'Effective billing status for this node; backs /api/billing/status.';
COMMENT ON COLUMN billing_config.receipt_row_fee_cents IS 'Fee in minor units (cents) charged per transactional row on receipt upload; default 10.';
COMMENT ON COLUMN billing_invoice.period_start IS 'Nominal label for the billing period; the sweep cut-off is period_end (all still-unbilled usage before it is included).';

COMMIT;
