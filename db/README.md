# SAMePOS licensing & trial-enforcement core (PostgreSQL)

The database core for SAMePOS node licensing: node identity, the standard trial,
Ed25519-signed licences, an effective-status view, and an optional hard
enforcement guard. Built for the SAMePOS node's embedded PostgreSQL (developed
and tested against PG 16).

> **This touches a venue's financial database.** The migration is additive and
> idempotent and never drops or alters existing product tables, but before you
> apply it to a live/trading node, reconcile it with whatever licence tables the
> product's earlier migrations (001–023) already create — this core was authored
> from the documented model, not against the product's current schema. It was
> verified against a throwaway cluster only; it has **not** been run against any
> real node.

## Reconciliation status — READ BEFORE DEPLOYING

This core has **not yet been diffed against the SAMePOS product's own migrations
(001–023)**; the product source is not available in the repositories this work
was done from. Until that diff happens, treat these as open:

1. **Name collisions.** If 001–023 already create a licence table/view/function
   with any of these names, this migration's `CREATE … IF NOT EXISTS` will
   silently adopt the *existing* object instead of the one defined here, and the
   shapes may not match. Check first:
   ```sql
   SELECT c.relkind, c.relname FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname LIKE '%licen%'
   UNION ALL
   SELECT 'f', p.proname FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname LIKE '%licen%';
   ```
   A non-empty result means reconcile before applying.
2. **Migration number.** `024` assumes 023 is the product's latest. Renumber if
   the product has moved on, and match its migration-runner/tracking convention.
3. **Payment table name** for the guard (see *Enforcement* below).
4. **Existing licence state.** If the node already stores a trial start or an
   activated key elsewhere, migrate that state in rather than letting
   `licence_ensure_trial()` start a fresh trial and silently extend the term.

### Run the pre-flight check

`db/tools/check_reconciliation.sql` answers all four questions above against a
real node. It is **strictly read-only** — it reads system catalogs and counts
rows, creating and modifying nothing — so it is safe on a live trading node:

```bash
psql -d <node_db> -f db/tools/check_reconciliation.sql > reconcile.txt
```

It reports: existing `licence`/`trial` objects and their columns, a direct
collision check against all 16 object names this migration creates (**any `t`
means reconcile first**), the migration-tracking table and its row count, which
payment table the guard should attach to, pre-existing licence state row counts,
and any guard trigger already installed.

Verified against a disposable PostgreSQL 16 cluster in three states: a bare
database, a database holding a *conflicting* `licence` table of a different
shape (correctly flagged), and a database with this migration already applied.
Object and row counts were identical before and after each run.

To complete the diff, send back that output — or the licence-related parts of
migrations 001–023, or `pg_dump -s -t 'licence*' -t 'trial*'` (schema only).

## Files

| File | Purpose |
|------|---------|
| `migrations/024_licensing.sql` | The licensing core as a numbered, idempotent, additive migration. |
| `migrations/024_licensing.down.sql` | Rollback — drops every object the migration creates (deletes all licence state). |
| `schema/licensing.schema.sql` | The **same objects** as a standalone consolidated schema for fresh builds / test DBs. Kept byte-identical to the migration body; `run_db_tests.sh` asserts equivalence. |
| `tests/behavior.sql` | Behavioural assertions (plpgsql `ASSERT`). |
| `tests/run_db_tests.sh` | Spins up a disposable PG cluster and runs the full verification. |
| `tools/check_reconciliation.sql` | **Read-only** pre-flight to run on a real node before applying (collision + guard-target report). |

## The model

A SAMePOS node is one venue box, identified by a machine-bound **install code**.
Licensing (per the `samepos-node-install` runbook) is:

- **Standard trial** — 14 days by default, auto-started on first boot.
- **Signed licences** — Ed25519 `SPOS1.` keys **bound to the node's install
  code**, activated via `POST /api/licence/activate`. The private signing key
  lives only in the vendor keystore.

**Crypto stays in the app.** PostgreSQL cannot verify Ed25519 signatures with
stock functions, so the FastAPI app verifies the signature with the vendor
public key and passes the *result* to the database. This schema is the
system-of-record and the enforcement point — not the verifier.

### Tables

| Table | Role |
|-------|------|
| `licence_config` | Singleton tuning row: `standard_trial_days` (14), `grace_days` (0), `hard_enforcement` (false). |
| `licence_node` | Singleton node identity: the `install_code`, first-boot time, app version, hostname. |
| `licence_trust_key` | Vendor Ed25519 **public** keys (provenance / rotation). |
| `licence` | Auto trials + signed licences, each bound to an `install_code`. |
| `licence_audit` | Append-only trail: `trial_start`, `activate`, `revoke`, `reject`. |

### Functions & view

| Object | What it does |
|--------|--------------|
| `licence_set_node(install_code[, app_version, hostname])` | Upsert the singleton node identity. |
| `licence_ensure_trial([days])` | Auto-start the standard trial on first boot; idempotent (no-op if the node already has any licence). |
| `licence_record_signed(mode, key, bound_install_code, expires_at, verified, …)` | Record an app-verified signed licence. Returns `(success, reason, licence_id)`. Enforces install-code binding and the `verified` flag; **rejections return `success=false`** (and are audited) rather than raising, so the reject trail persists. A `licence` row is written only on success. |
| `licence_revoke(licence_id[, reason])` | Revoke a licence. |
| `licence_effective_status()` → `licence_status` (view) | The core. Computes the governing licence and returns `mode`, `state` (`trial`/`extended_trial`/`licensed`/`grace`/`expired`/`none`), `is_valid`, `in_grace`, `expires_at`, `days_remaining`, etc. Precedence: valid **licensed** > valid **extended_trial** > valid **trial**. |
| `licence_is_valid()` | Boolean helper used by the guard. |
| `licence_guard_sales()` / `licence_attach_guard(table)` / `licence_attach_guard_auto()` | The optional hard guard (see below). |

### Concurrency

`licence_ensure_trial()` takes `pg_advisory_xact_lock(hashtext(install_code))`
and re-reads under the lock before inserting, and a partial unique index
(`licence_one_auto_trial_uidx`) is the database-level backstop. Without this, the
several tablets that hit a node at first boot can each pass a check-then-insert
and create duplicate trials — the same race the product hit on `business_day`.

## App integration

```text
first boot            -> SELECT licence_set_node(:install_code, :app_version, :hostname);
                         SELECT licence_ensure_trial();
GET  /api/licence/status  -> SELECT * FROM licence_status;
POST /api/licence/activate -> app verifies the Ed25519 key, then:
        SELECT success, reason, licence_id
          FROM licence_record_signed(:mode, :key, :bound_code, :expires_at, :verified, ...);
        -- return HTTP 400 with `reason` when success is false
turn on hard blocking -> UPDATE licence_config SET hard_enforcement = true;
```

Trial length also honours the product's `SAMEPOS_TRIAL_STANDARD_DAYS` env var:
pass it as `licence_ensure_trial(:days)`, or keep it in `licence_config`.

## Enforcement: advisory by default, optional hard guard

Enforcement is **advisory** — the app reads `licence_status` and decides what to
do (e.g. refuse to open a tab, show a banner). This is the recommended posture:
the FastAPI app owns the user-facing behaviour.

An **optional hard guard** is also installed: a `BEFORE INSERT` trigger that
blocks **payments** when the licence is invalid. It is **gated by
`licence_config.hard_enforcement`, which ships `false`**, so it does nothing
until an admin turns it on:

```sql
UPDATE licence_config SET hard_enforcement = true;   -- start blocking payments when invalid
UPDATE licence_config SET hard_enforcement = false;  -- back to advisory only
```

**Why payment and not tab-open.** `sales_order` rows are created when staff open
a tab (the runbook's gotcha #4 is a NOT NULL violation on
`sales_order.business_day_id` at `POST /api/orders`). Guarding that insert would
stop staff opening or continuing tabs and could strand an open shift
mid-service. Guarding the payment insert lets an in-progress shift keep serving
while preventing the node from taking money on a dead licence.

The migration attaches the guard via `licence_attach_guard_auto()`, which probes
`sales_payment`, `sale_payment`, `order_payment`, `payment` and uses the first
that exists. **The product's real payment table name is not pinned down by the
runbook** — if it differs, attach explicitly:

```sql
SELECT licence_attach_guard('public.<payment_table>');
```

If no payment table exists when the migration runs, the guard is not attached
and the migration says so in a `NOTICE`.

## Applying

As a numbered migration (via the product's migration runner, after 023):

```bash
psql -d <node_db> -f db/migrations/024_licensing.sql
# rollback:
psql -d <node_db> -f db/migrations/024_licensing.down.sql
```

Or standalone, on any database:

```bash
psql -d <db> -f db/schema/licensing.schema.sql
```

Both are wrapped in a transaction and are safe to re-run.

## Testing

The harness builds its own disposable PostgreSQL cluster (never touching a real
DB) and verifies migration + standalone apply, behaviour, idempotency,
migration/standalone equivalence, and rollback:

```bash
bash db/tests/run_db_tests.sh          # exits 0 on pass, 77 if no PG server binaries
python3 -m pytest tests/test_licensing_db.py   # same, integrated into the pytest suite
```

Requires PostgreSQL **server** binaries (`initdb`, `pg_ctl`); the harness runs
the cluster as an unprivileged user when invoked as root.
