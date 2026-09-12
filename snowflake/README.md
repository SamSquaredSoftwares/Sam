# Read-only Snowflake role

`query_snowflake` refuses non-read-only SQL in the application, but that check
is a guardrail, not a security boundary — any application-level SQL filter can
be worked around. **This role is the boundary.** It holds only `USAGE` and
`SELECT`, so Snowflake itself refuses every write, whatever SQL reaches it.

## Apply it

```bash
# 1. Review the SQL (prints by default -- these are privilege changes)
.venv/bin/python scripts/snowflake_readonly_role.py \
  --role SAM_READONLY --user SAM_SERVICE \
  --warehouse COMPUTE_WH --database ANALYTICS

# 2. Apply it, as an admin who can create roles and owns the objects.
#    Repeat the flags from step 1: --execute re-renders from whatever it is
#    given, so running it bare applies different SQL than you just read. It
#    prints the statements and asks before running them (--yes skips the
#    prompt, and is required when stdin is not a terminal).
export SNOWFLAKE_ACCOUNT=myorg-myaccount
export SNOWFLAKE_ADMIN_USER=...       # SECURITYADMIN + object ownership
export SNOWFLAKE_ADMIN_PASSWORD=...
.venv/bin/python scripts/snowflake_readonly_role.py \
  --role SAM_READONLY --user SAM_SERVICE \
  --warehouse COMPUTE_WH --database ANALYTICS --execute

# 3. Point the Action Server at it
echo 'SNOWFLAKE_ROLE=SAM_READONLY' >> .env

# 4. Prove it is actually read-only
.venv/bin/python scripts/verify_snowflake_readonly.py
```

Both scripts read `.env` as well as the environment, so step 4 picks up the
`SNOWFLAKE_ROLE` written in step 3.

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

`scripts/verify_snowflake_readonly.py` connects as the role and proves three
things, exiting non-zero unless all three hold, so it can gate a deploy:

1. **Reads work** — `SELECT` succeeds, so the Action Server is actually usable.
2. **Every grant on the role is read-only** — it reads `SHOW GRANTS TO ROLE`
   and fails on any privilege beyond `USAGE`/`SELECT`/`REFERENCES`/`MONITOR`/
   `OPERATE`, and on any *inherited* role, whose privileges would apply here
   too.
3. **Writes are refused** — `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE` and
   `CREATE SCHEMA` are all rejected on privileges.

Two details in (3) decide whether it proves anything at all.

**The DML probes have to hit a table that exists.** Snowflake answers a write
against a missing table with `Object '...' does not exist or not authorized` —
the identical answer a role with full write access gets. A probe aimed at a
table that does not exist is therefore not evidence, whatever the error says.
So the verifier finds a real base table through `INFORMATION_SCHEMA` (override
with `--probe-table DB.SCHEMA.TABLE`) and probes that. Nothing is written to
it: each statement carries `where 1 = 0` and the lot runs inside a transaction
that is rolled back, so the probes stay harmless even against a role that turns
out to be able to write.

**Anything short of a privilege refusal is a failure.** A probe that fails
because the object was missing, or for a reason the script does not recognise,
proves nothing — so it fails the run and prints the error, rather than warning
and exiting 0.

There is deliberately no `DROP TABLE` probe. It cannot be made harmless the way
the DML probes can, and aiming it at a table that does not exist is what made
the earlier version of this script useless. `SHOW GRANTS` covers it instead: a
role can only drop what it owns, and check (2) fails on `OWNERSHIP`.
