-- ===========================================================================
-- Read-only Snowflake role for the Sam Action Server.
--
-- `query_snowflake` refuses non-read-only SQL in the application, but that is
-- a guardrail, not a security boundary. THIS is the boundary: a role that
-- holds only USAGE and SELECT, so the warehouse itself refuses every write.
--
-- Placeholders (replace all four, or render this file with
-- scripts/snowflake_readonly_role.py, which validates and substitutes them):
--
--   <ROLE>        role to create, e.g. SAM_READONLY
--   <USER>        the service user the Action Server authenticates as
--   <WAREHOUSE>   warehouse the role may run queries on
--   <DATABASE>    database the role may read
--
-- Apply with SnowSQL, the Snowflake web UI, or:
--   python scripts/snowflake_readonly_role.py --execute
--
-- Re-running is safe: every statement is idempotent.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Create the role.
--    Roles are account-level objects; SECURITYADMIN owns role management.
-- ---------------------------------------------------------------------------
USE ROLE SECURITYADMIN;

CREATE ROLE IF NOT EXISTS <ROLE>
    COMMENT = 'Read-only access for the Sam Action Server (query_snowflake).';


-- ---------------------------------------------------------------------------
-- 2. Grant read access.
--    These grants must come from a role that OWNS the objects (commonly
--    SYSADMIN) or that holds MANAGE GRANTS. If your objects are owned by a
--    different role, switch to it here instead of SYSADMIN.
-- ---------------------------------------------------------------------------
USE ROLE SYSADMIN;

-- Compute. USAGE is enough to run queries. Add OPERATE only if you want the
-- role to be able to resume a suspended warehouse itself (not needed when the
-- warehouse has AUTO_RESUME = TRUE).
GRANT USAGE ON WAREHOUSE <WAREHOUSE> TO ROLE <ROLE>;

-- Traversal: a role cannot see a schema without USAGE on its database.
GRANT USAGE ON DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT USAGE ON ALL SCHEMAS IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE <DATABASE> TO ROLE <ROLE>;

-- Reads. "ALL" covers what exists today; "FUTURE" covers what gets created
-- later, so the role does not silently go blind as the schema evolves.
GRANT SELECT ON ALL TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT SELECT ON FUTURE TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;

GRANT SELECT ON ALL VIEWS IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT SELECT ON FUTURE VIEWS IN DATABASE <DATABASE> TO ROLE <ROLE>;

-- The object types below may not exist in every account or edition.
-- Materialized views are Enterprise+; dynamic tables need a recent Snowflake.
-- If any statement errors with an unsupported object type, delete that line --
-- it grants nothing you otherwise need.
GRANT SELECT ON ALL MATERIALIZED VIEWS IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT SELECT ON FUTURE MATERIALIZED VIEWS IN DATABASE <DATABASE> TO ROLE <ROLE>;

GRANT SELECT ON ALL EXTERNAL TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT SELECT ON FUTURE EXTERNAL TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;

GRANT SELECT ON ALL DYNAMIC TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;
GRANT SELECT ON FUTURE DYNAMIC TABLES IN DATABASE <DATABASE> TO ROLE <ROLE>;


-- ---------------------------------------------------------------------------
-- 3. Attach the role to the service user.
-- ---------------------------------------------------------------------------
USE ROLE SECURITYADMIN;

GRANT ROLE <ROLE> TO USER <USER>;

-- Make it the default so an accidental connection without an explicit role
-- still lands read-only rather than on something more privileged.
ALTER USER <USER> SET DEFAULT_ROLE = <ROLE>;
ALTER USER <USER> SET DEFAULT_WAREHOUSE = <WAREHOUSE>;
