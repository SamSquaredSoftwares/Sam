# SAMePOS licensing & monetization cores (PostgreSQL)

The database cores for the SAMePOS node, built for its embedded PostgreSQL
(developed and tested against PG 16):

- **Licensing core** (`024_licensing.sql`) — node identity, the standard
  trial, Ed25519-signed licences, an effective-status view, and an optional
  hard enforcement guard.
- **Monetization core** (`025_monetization.sql`, requires 024) — usage
  billing at a fee per transactional row on receipt uploads (default 10c),
  the once-off licensing & integration service charge (quote → confirm), the
  permanent machine binding that makes the product non-transferable after
  confirmation, invoicing, and a billing status view. Full model and analysis:
  [`docs/monetization.md`](../docs/monetization.md).

> **This touches a venue's financial database.** The migration is additive and
> idempotent and never drops or alters existing product tables, but before you
> apply it to a live/trading node, reconcile it with whatever licence tables the
> product's earlier migrations (001–023) already create — this core was authored
> from the documented model, not against the product's current schema. It was
> verified against a throwaway cluster only; it has **not** been run against any
> real node.

## Files

| File | Purpose |
|------|---------|
| `migrations/024_licensing.sql` | The licensing core as a numbered, idempotent, additive migration. |
| `migrations/024_licensing.down.sql` | Rollback — drops every object the migration creates (deletes all licence state). |
| `migrations/025_monetization.sql` | The monetization core (usage billing + once-off charge + machine lock); requires 024. |
| `migrations/025_monetization.down.sql` | Rollback — drops every billing object (deletes all billing state; run **before** 024's rollback). |
| `schema/licensing.schema.sql` | The **same objects** as the 024 migration, as a standalone consolidated schema for fresh builds / test DBs. Kept byte-identical to the migration body; `run_db_tests.sh` asserts equivalence. |
| `schema/monetization.schema.sql` | Standalone consolidated schema for the monetization core (apply after `licensing.schema.sql`); equivalence enforced the same way. |
| `tests/behavior.sql` | Licensing behavioural assertions (plpgsql `ASSERT`). |
| `tests/behavior_monetization.sql` | Monetization behavioural assertions (charging, idempotency, quote→confirm flow, binding immutability incl. TRUNCATE, invoicing, void/re-bill, stranded-usage sweep). |
| `tests/run_db_tests.sh` | Spins up a disposable PG cluster and runs the full verification of both cores. |

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
| `licence_guard_sales()` / `licence_attach_guard([table])` | The optional hard guard (see below). |

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

An **optional hard guard** is also installed: a `BEFORE INSERT` trigger on
`public.sales_order` that blocks new sales when the licence is invalid. It is
**gated by `licence_config.hard_enforcement`, which ships `false`**, so it does
nothing until an admin turns it on:

```sql
UPDATE licence_config SET hard_enforcement = true;   -- start blocking sales when invalid
UPDATE licence_config SET hard_enforcement = false;  -- back to advisory only
```

If `public.sales_order` doesn't exist when the migration runs, the guard is not
attached; run `SELECT licence_attach_guard();` once the sales schema is present
(or `SELECT licence_attach_guard('public.other_table');` to guard a different
entry point).

## The monetization core (025)

Layered on top of licensing, `025_monetization.sql` adds how the product earns:

| Table | Role |
|-------|------|
| `billing_config` | Singleton tuning row: `currency` (ZAR), `receipt_row_fee_cents` (10). |
| `billing_machine_binding` | The permanent machine lock, written when the once-off charge is confirmed. Immutable by trigger (no update, no delete) — the product is **non-transferable**. |
| `billing_receipt_batch` | Usage ledger: one row per generated-receipts upload, charged per transactional row at the snapshotted rate (`amount_cents` is a generated column). |
| `billing_service_charge` | The once-off licensing & integration fee (amount may be NULL while still to be confirmed). |
| `billing_invoice` | Issued bills: usage roll-up + the once-off charge. |
| `billing_audit` | Append-only money/binding trail. |

| Object | What it does |
|--------|--------------|
| `billing_record_receipt_upload(batch_ref, row_count, …)` | Meter an upload at the per-row fee. Idempotent on `batch_ref` (a replay returns the original charge — never double-billed); rejects machine mismatches and row-count conflicts with `success=false` + audit. |
| `billing_quote_service_charge([amount_cents, notes])` | Quote the once-off fee; amount may be NULL while TBC; re-quote re-prices only a pending quote. |
| `billing_confirm_service_charge(machine_id[, amount_cents, …])` | Confirm the once-off fee **and permanently bind the product to that machine id**. Refuses to confirm without an amount, to re-price silently, or to bind a second machine. |
| `billing_generate_invoice([period_start, period_end))` | Roll unbilled usage + the confirmed once-off charge into one issued invoice (defaults to the previous month). Sweeps **all** still-unbilled usage before `period_end` so a missed run cannot strand revenue; stamps and totals batches in one statement; serialised per node by an advisory lock. |
| `billing_mark_invoice_paid(invoice_id[, reference])` | Settle an invoice (settles the once-off charge with it). |
| `billing_void_invoice(invoice_id[, reason])` | Void an issued invoice and release its batches + once-off charge back to unbilled so they are re-billed (voiding by hand would strand them). |
| `billing_machine_ok(machine_id)` | Boot check: true when unbound or bound to this machine. |
| `billing_effective_status()` → `billing_status` (view) | Binding, charge state, rate, unbilled accrual, outstanding balance — backs `/api/billing/status`. |

Triggers enforce the lock: `billing_machine_binding` is write-once
(`trg_billing_binding_immutable` — no update, no delete), `BEFORE TRUNCATE`
guards cover the route row-level triggers miss (on the binding and every money
table), once bound `licence_node.install_code` can no longer change
(`trg_billing_lock_install_code`), and an invoiced once-off charge cannot be
cancelled out from under its invoice (`trg_billing_service_charge_guard`).

See [`docs/monetization.md`](../docs/monetization.md) for the full revenue
model, worked examples, and the app integration checklist.

## Applying

As numbered migrations (via the product's migration runner, after 023; 025
requires 024):

```bash
psql -d <node_db> -f db/migrations/024_licensing.sql
psql -d <node_db> -f db/migrations/025_monetization.sql
# rollback (reverse order):
psql -d <node_db> -f db/migrations/025_monetization.down.sql
psql -d <node_db> -f db/migrations/024_licensing.down.sql
```

Or standalone, on any database:

```bash
psql -d <db> -f db/schema/licensing.schema.sql
psql -d <db> -f db/schema/monetization.schema.sql
```

All are wrapped in a transaction and are safe to re-run.

## Testing

The harness builds its own disposable PostgreSQL cluster (never touching a real
DB) and verifies migration + standalone apply, behaviour of both cores,
idempotency, migration/standalone equivalence, and rollback:

```bash
bash db/tests/run_db_tests.sh          # exits 0 on pass, 77 if no PG server binaries
python3 -m pytest tests/test_licensing_db.py tests/test_monetization_db.py
```

Requires PostgreSQL **server** binaries (`initdb`, `pg_ctl`); the harness runs
the cluster as an unprivileged user when invoked as root.
