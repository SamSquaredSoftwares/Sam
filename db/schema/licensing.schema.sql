-- ===========================================================================
-- licensing.schema.sql
-- SAMePOS licensing & trial-enforcement core — STANDALONE consolidated schema.
--
-- This is the same set of objects as db/migrations/024_licensing.sql, packaged
-- as a standalone schema you can apply to any PostgreSQL database to stand up
-- the licensing core on its own (e.g. a fresh build or a test DB):
--
--     psql -d <db> -f db/schema/licensing.schema.sql
--
-- It is idempotent and additive. When a public.sales_order table is present the
-- optional hard-guard trigger is attached automatically (disabled by default);
-- otherwise run  SELECT licence_attach_guard();  once that table exists.
--
-- Equivalence with the numbered migration is enforced by db/tests/run_db_tests.sh.
-- See db/README.md for the full model and app integration points.
-- ===========================================================================

BEGIN;

-- --- licence_mode enum ------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'licence_mode') THEN
        CREATE TYPE licence_mode AS ENUM ('trial', 'extended_trial', 'licensed');
    END IF;
END$$;

-- --- licence_config: singleton tuning row -----------------------------------
CREATE TABLE IF NOT EXISTS licence_config (
    id                  smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    standard_trial_days integer     NOT NULL DEFAULT 14 CHECK (standard_trial_days > 0),
    grace_days          integer     NOT NULL DEFAULT 0  CHECK (grace_days >= 0),
    hard_enforcement    boolean     NOT NULL DEFAULT false,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO licence_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- --- licence_node: singleton node identity (the machine-bound install code) -
CREATE TABLE IF NOT EXISTS licence_node (
    id            smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    install_code  text        NOT NULL UNIQUE,
    first_boot_at timestamptz NOT NULL DEFAULT now(),
    app_version   text,
    hostname      text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- --- licence_trust_key: vendor Ed25519 PUBLIC keys (provenance / rotation) --
CREATE TABLE IF NOT EXISTS licence_trust_key (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key_id     text        NOT NULL UNIQUE,     -- vendor key id / "kid"
    algorithm  text        NOT NULL DEFAULT 'ed25519',
    public_key text        NOT NULL,            -- base64/hex of the raw public key
    active     boolean     NOT NULL DEFAULT true,
    added_at   timestamptz NOT NULL DEFAULT now()
);

-- --- licence: auto trials + signed licences ---------------------------------
CREATE TABLE IF NOT EXISTS licence (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode            licence_mode NOT NULL,
    install_code    text         NOT NULL,       -- install code this licence is bound to
    key_string      text,                        -- raw "SPOS1." key; NULL for auto trial
    key_fingerprint text,                        -- sha256(key_string) hex; for dedupe/audit
    source          text         NOT NULL DEFAULT 'api'
                                  CHECK (source IN ('auto', 'api', 'manual')),
    trust_key_id    bigint       REFERENCES licence_trust_key(id),
    signature       text,                        -- base64 signature (signed licences)
    signed_payload  jsonb,                       -- signed claims (expiry, features, ...)
    verified        boolean      NOT NULL DEFAULT false,
    issued_at       timestamptz,
    starts_at       timestamptz  NOT NULL DEFAULT now(),
    expires_at      timestamptz,                 -- NULL => perpetual (licensed)
    trial_days      integer,
    activated_at    timestamptz  NOT NULL DEFAULT now(),
    activated_by    text,
    revoked_at      timestamptz,
    revoked_reason  text,
    notes           text,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT licence_expiry_after_start
        CHECK (expires_at IS NULL OR expires_at > starts_at),
    CONSTRAINT licence_trial_has_no_key
        CHECK (mode <> 'trial' OR key_string IS NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS licence_key_fingerprint_uidx
    ON licence (key_fingerprint) WHERE key_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS licence_install_active_idx
    ON licence (install_code, revoked_at, expires_at);

-- --- licence_audit: append-only lifecycle audit -----------------------------
CREATE TABLE IF NOT EXISTS licence_audit (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    action          text        NOT NULL,   -- trial_start|activate|revoke|reject|config_change
    install_code    text,
    licence_id      bigint,
    key_fingerprint text,
    outcome         text        NOT NULL DEFAULT 'ok',   -- ok|rejected|error
    detail          jsonb
);

-- ===========================================================================
-- Functions
-- ===========================================================================

-- Upsert the singleton node identity.
CREATE OR REPLACE FUNCTION licence_set_node(
    p_install_code text,
    p_app_version  text DEFAULT NULL,
    p_hostname     text DEFAULT NULL
) RETURNS licence_node
LANGUAGE plpgsql AS $$
DECLARE r licence_node;
BEGIN
    IF p_install_code IS NULL OR length(btrim(p_install_code)) = 0 THEN
        RAISE EXCEPTION 'install_code must be a non-empty string';
    END IF;
    INSERT INTO licence_node (id, install_code, app_version, hostname)
    VALUES (1, btrim(p_install_code), p_app_version, p_hostname)
    ON CONFLICT (id) DO UPDATE
        SET install_code = EXCLUDED.install_code,
            app_version  = COALESCE(EXCLUDED.app_version, licence_node.app_version),
            hostname     = COALESCE(EXCLUDED.hostname, licence_node.hostname),
            updated_at   = now()
    RETURNING * INTO r;
    RETURN r;
END$$;

-- Auto-start the standard trial on first boot, only if the node has no licence.
CREATE OR REPLACE FUNCTION licence_ensure_trial(p_days integer DEFAULT NULL)
RETURNS licence
LANGUAGE plpgsql AS $$
DECLARE
    v_node licence_node;
    v_days integer;
    v_lic  licence;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;

    IF EXISTS (SELECT 1 FROM licence WHERE install_code = v_node.install_code) THEN
        SELECT * INTO v_lic FROM licence
         WHERE install_code = v_node.install_code
         ORDER BY created_at DESC, id DESC LIMIT 1;
        RETURN v_lic;
    END IF;

    v_days := COALESCE(p_days,
                       (SELECT standard_trial_days FROM licence_config WHERE id = 1),
                       14);

    INSERT INTO licence (mode, install_code, source, verified,
                         starts_at, expires_at, trial_days, activated_by, notes)
    VALUES ('trial', v_node.install_code, 'auto', true,
            now(), now() + make_interval(days => v_days), v_days,
            'system', 'auto-started standard trial')
    RETURNING * INTO v_lic;

    INSERT INTO licence_audit (action, install_code, licence_id, outcome, detail)
    VALUES ('trial_start', v_node.install_code, v_lic.id, 'ok',
            jsonb_build_object('trial_days', v_days));

    RETURN v_lic;
END$$;

-- Record an app-verified signed licence. Enforces install-code binding and
-- refuses anything the app did not verify. Crypto is the app's job; this is the
-- system-of-record.
--
-- Business rejections (unverified signature / install-code mismatch) are
-- returned as success=false with a reason, NOT raised. This is deliberate:
-- PostgreSQL has no autonomous transactions, so a RAISE would roll back the
-- reject audit row we want to keep. Returning a status lets the reject audit
-- commit and gives the app a clean value to turn into an HTTP 4xx. A row is
-- inserted into `licence` only on success, so a caller that ignores the status
-- still cannot end up with a bad licence. Genuine programming errors (no node
-- identity, wrong mode) still raise.
CREATE OR REPLACE FUNCTION licence_record_signed(
    p_mode               licence_mode,   -- 'licensed' or 'extended_trial'
    p_key_string         text,
    p_bound_install_code text,           -- install code parsed from the key by the app
    p_expires_at         timestamptz,    -- NULL for perpetual 'licensed'
    p_verified           boolean,        -- the app's Ed25519 verification result
    p_trust_key_id       bigint      DEFAULT NULL,
    p_signature          text        DEFAULT NULL,
    p_signed_payload     jsonb       DEFAULT NULL,
    p_issued_at          timestamptz DEFAULT NULL,
    p_activated_by       text        DEFAULT NULL
) RETURNS TABLE (success boolean, reason text, licence_id bigint)
LANGUAGE plpgsql AS $$
DECLARE
    v_node licence_node;
    v_fp   text;
    v_lic  licence;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    IF v_node.install_code IS NULL THEN
        RAISE EXCEPTION 'node identity not set; call licence_set_node(install_code) first';
    END IF;
    IF p_mode = 'trial' THEN
        RAISE EXCEPTION 'licence_record_signed is for signed licences; use licence_ensure_trial() for trials';
    END IF;

    v_fp := encode(sha256(convert_to(COALESCE(p_key_string, ''), 'UTF8')), 'hex');

    IF NOT COALESCE(p_verified, false) THEN
        INSERT INTO licence_audit (action, install_code, key_fingerprint, outcome, detail)
        VALUES ('reject', v_node.install_code, v_fp, 'rejected',
                jsonb_build_object('reason', 'signature not verified'));
        RETURN QUERY SELECT false, 'signature not verified'::text, NULL::bigint;
        RETURN;
    END IF;

    IF p_bound_install_code IS DISTINCT FROM v_node.install_code THEN
        INSERT INTO licence_audit (action, install_code, key_fingerprint, outcome, detail)
        VALUES ('reject', v_node.install_code, v_fp, 'rejected',
                jsonb_build_object('reason', 'install code mismatch',
                                   'bound_to', p_bound_install_code));
        RETURN QUERY SELECT false,
                            format('install code mismatch: key bound to %s, node is %s',
                                   p_bound_install_code, v_node.install_code),
                            NULL::bigint;
        RETURN;
    END IF;

    INSERT INTO licence (mode, install_code, key_string, key_fingerprint, source,
                         trust_key_id, signature, signed_payload, verified,
                         issued_at, starts_at, expires_at, activated_by)
    VALUES (p_mode, v_node.install_code, p_key_string, v_fp, 'api',
            p_trust_key_id, p_signature, p_signed_payload, true,
            p_issued_at, now(), p_expires_at, p_activated_by)
    ON CONFLICT (key_fingerprint) WHERE key_fingerprint IS NOT NULL
    DO UPDATE SET revoked_at     = NULL,
                  revoked_reason = NULL,
                  expires_at     = EXCLUDED.expires_at,
                  signed_payload = EXCLUDED.signed_payload,
                  verified       = true,
                  activated_at   = now()
    RETURNING * INTO v_lic;

    INSERT INTO licence_audit (action, install_code, licence_id, key_fingerprint, outcome, detail)
    VALUES ('activate', v_node.install_code, v_lic.id, v_fp, 'ok',
            jsonb_build_object('mode', p_mode, 'expires_at', p_expires_at));

    RETURN QUERY SELECT true, 'activated'::text, v_lic.id;
END$$;

-- Revoke a licence.
CREATE OR REPLACE FUNCTION licence_revoke(p_licence_id bigint, p_reason text DEFAULT NULL)
RETURNS licence
LANGUAGE plpgsql AS $$
DECLARE v_lic licence;
BEGIN
    UPDATE licence
       SET revoked_at = now(), revoked_reason = p_reason
     WHERE id = p_licence_id AND revoked_at IS NULL
    RETURNING * INTO v_lic;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'licence % not found or already revoked', p_licence_id;
    END IF;
    INSERT INTO licence_audit (action, install_code, licence_id, outcome, detail)
    VALUES ('revoke', v_lic.install_code, v_lic.id, 'ok',
            jsonb_build_object('reason', p_reason));
    RETURN v_lic;
END$$;

-- The core: compute the node's effective licence status.
CREATE OR REPLACE FUNCTION licence_effective_status()
RETURNS TABLE (
    install_code   text,
    mode           text,       -- governing licence's stored mode, or 'none'
    state          text,       -- trial|extended_trial|licensed|grace|expired|none
    is_valid       boolean,
    in_grace       boolean,
    starts_at      timestamptz,
    expires_at     timestamptz,
    days_remaining integer,     -- NULL for perpetual
    licence_id     bigint,
    source         text,
    checked_at     timestamptz
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_node  licence_node;
    v_grace integer;
    v_now   timestamptz := now();
    v_rec   record;
BEGIN
    SELECT * INTO v_node FROM licence_node WHERE id = 1;
    v_grace := COALESCE((SELECT grace_days FROM licence_config WHERE id = 1), 0);

    IF v_node.install_code IS NULL THEN
        RETURN QUERY SELECT NULL::text, 'none'::text, 'none'::text, false, false,
                            NULL::timestamptz, NULL::timestamptz, NULL::integer,
                            NULL::bigint, NULL::text, v_now;
        RETURN;
    END IF;

    SELECT l.*,
           (l.expires_at IS NULL
              OR l.expires_at + make_interval(days => v_grace) > v_now) AS within_validity,
           (l.expires_at IS NOT NULL
              AND l.expires_at <= v_now
              AND l.expires_at + make_interval(days => v_grace) > v_now) AS is_grace
      INTO v_rec
      FROM licence l
     WHERE l.install_code = v_node.install_code
       AND l.revoked_at IS NULL
       AND l.verified = true
     ORDER BY
        (l.expires_at IS NULL
           OR l.expires_at + make_interval(days => v_grace) > v_now) DESC,
        CASE l.mode WHEN 'licensed' THEN 3
                    WHEN 'extended_trial' THEN 2
                    WHEN 'trial' THEN 1 ELSE 0 END DESC,
        COALESCE(l.expires_at, 'infinity'::timestamptz) DESC,
        l.id DESC
     LIMIT 1;

    IF NOT FOUND THEN
        RETURN QUERY SELECT v_node.install_code, 'none'::text, 'none'::text, false, false,
                            NULL::timestamptz, NULL::timestamptz, NULL::integer,
                            NULL::bigint, NULL::text, v_now;
        RETURN;
    END IF;

    RETURN QUERY SELECT
        v_node.install_code,
        v_rec.mode::text,
        CASE
            WHEN v_rec.within_validity AND v_rec.is_grace THEN 'grace'
            WHEN v_rec.within_validity THEN v_rec.mode::text
            ELSE 'expired'
        END,
        v_rec.within_validity,
        COALESCE(v_rec.is_grace, false),
        v_rec.starts_at,
        v_rec.expires_at,
        CASE WHEN v_rec.expires_at IS NULL THEN NULL
             ELSE GREATEST(0, CEIL(EXTRACT(EPOCH FROM (v_rec.expires_at - v_now)) / 86400.0))::integer
        END,
        v_rec.id,
        v_rec.source,
        v_now;
END$$;

-- Convenience view (what /api/licence/status should return).
CREATE OR REPLACE VIEW licence_status AS SELECT * FROM licence_effective_status();

-- Boolean helper used by the optional hard guard.
CREATE OR REPLACE FUNCTION licence_is_valid()
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE((SELECT is_valid FROM licence_effective_status()), false);
$$;

-- ===========================================================================
-- Optional hard guard (advisory by default; gated by licence_config)
-- ===========================================================================

-- BEFORE INSERT trigger function: blocks a new sale only when hard enforcement
-- is switched on AND the licence is invalid. Shipped OFF (hard_enforcement=false).
CREATE OR REPLACE FUNCTION licence_guard_sales()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE((SELECT hard_enforcement FROM licence_config WHERE id = 1), false)
       AND NOT licence_is_valid() THEN
        RAISE EXCEPTION
            'SAMePOS licence invalid or expired; new sales are blocked (hard enforcement is ON). See /api/licence/status.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END$$;

-- Attach the guard to a sales table (default public.sales_order). Idempotent.
CREATE OR REPLACE FUNCTION licence_attach_guard(p_table text DEFAULT 'public.sales_order')
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF to_regclass(p_table) IS NULL THEN
        RAISE EXCEPTION 'table % does not exist; cannot attach licence guard', p_table;
    END IF;
    EXECUTE format('DROP TRIGGER IF EXISTS trg_licence_guard_sales ON %s', p_table);
    EXECUTE format(
        'CREATE TRIGGER trg_licence_guard_sales BEFORE INSERT ON %s '
        'FOR EACH ROW EXECUTE FUNCTION licence_guard_sales()', p_table);
END$$;

-- Attach now if the sales table already exists; otherwise leave a breadcrumb.
DO $$
BEGIN
    IF to_regclass('public.sales_order') IS NOT NULL THEN
        PERFORM licence_attach_guard('public.sales_order');
        RAISE NOTICE 'licence hard-guard attached to public.sales_order (gated by licence_config.hard_enforcement, currently OFF).';
    ELSE
        RAISE NOTICE 'public.sales_order not found; hard-guard NOT attached. Run SELECT licence_attach_guard(); after the sales schema exists.';
    END IF;
END$$;

COMMENT ON TABLE  licence               IS 'SAMePOS licences (auto trials + Ed25519-signed keys). Licensing core v1.';
COMMENT ON VIEW   licence_status        IS 'Effective licence status for this node; backs /api/licence/status.';
COMMENT ON COLUMN licence_config.hard_enforcement IS 'When true, the sales guard trigger blocks new sales while the licence is invalid.';

COMMIT;
