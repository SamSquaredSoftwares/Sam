# Read-only Snowflake role

`query_snowflake` refuses non-read-only SQL in the application, but that check
is a guardrail, not a security boundary — any application-level SQL filter can
be worked around. **This role is the boundary.** It holds only `USAGE` and
`SELECT`, so Snowflake itself refuses every write, whatever SQL reaches it.

## Apply it

```bash
# 1. Review the SQL (prints by default -- these are privilege changes)
python scripts/snowflake_readonly_role.py \
  --role SAM_READONLY --user SAM_SERVICE \
  --warehouse COMPUTE_WH --database ANALYTICS

# 2. Apply it, as an admin who can create roles and owns the objects
export SNOWFLAKE_ACCOUNT=myorg-myaccount
export SNOWFLAKE_ADMIN_USER=...       # SECURITYADMIN + object ownership
export SNOWFLAKE_ADMIN_PASSWORD=...
python scripts/snowflake_readonly_role.py --execute

# 3. Point the Action Server at it
echo 'SNOWFLAKE_ROLE=SAM_READONLY' >> .env

# 4. Prove it is actually read-only
python scripts/verify_snowflake_readonly.py
```

You can equally paste [`readonly_role.sql`](readonly_role.sql) into SnowSQL or
the Snowflake web UI after replacing the four placeholders yourself. Every
statement is idempotent, so re-running is safe.

Identifiers passed to the renderer are validated as bare Snowflake identifiers
before substitution, so a name can never inject extra DDL into the script.

## Four things that decide whether this actually protects you

**1. The service user must hold no other role.** A read-only role grants
nothing if the same user also holds `SYSADMIN` — the connection could simply
ask for the other role. Check what the user can reach:

```sql
SHOW GRANTS TO USER SAM_SERVICE;
```

The only roles listed should be `SAM_READONLY` and `PUBLIC`. Revoke anything
else, and prefer a dedicated service user over a human's account.

**2. `PUBLIC` is inherited by everyone.** Every user holds `PUBLIC`, so any
privilege granted to `PUBLIC` is one your service user has too. If someone has
granted write access there, this role does not stop it:

```sql
SHOW GRANTS TO ROLE PUBLIC;
```

**3. Future grants cover new objects, but schema-level beats database-level.**
The script grants on `FUTURE` objects in the database so the role does not go
blind as tables are added. If someone later adds a *schema-level* future grant
for the same object type, it takes precedence over the database-level one and
the role may miss new tables in that schema. Re-run the script (it is
idempotent) or add the schema-level grant explicitly.

**4. Multiple databases need multiple runs.** The grants are scoped to one
database. Re-run with `--database` for each one the agent should read.

## What the verifier checks

`scripts/verify_snowflake_readonly.py` connects as the role and asserts that
reads succeed and that `CREATE TABLE`, `INSERT`, `UPDATE`, `DELETE`,
`DROP TABLE`, and `CREATE SCHEMA` are all refused. It distinguishes a
*privilege* error from a "table does not exist" error — only the former proves
the role is safe — and exits non-zero on failure so it can gate a deploy.
