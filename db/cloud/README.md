# SAMePOS cloud POS trading schema (Supabase)

The cloud trading mirror for SAMePOS, as a tracked, additive, idempotent
migration with a rollback and a disposable-cluster test harness. Designed
for the SAMePOS Supabase project (ref `kpjqfsbjksnmxogmmcrb`), whose public
schema today holds only the company registry — the 25 Aug 2026 dump in
[`docs/samepos-project/knowledge/SCHEMA.sql`](../../docs/samepos-project/knowledge/SCHEMA.sql)
established that **no POS trading tables exist in the cloud yet**. This
directory is the designed answer.

> **NOT APPLIED to the live project.** This migration is designed and
> verified against a disposable cluster only. Applying it to the Supabase
> project is a deliberate, separate step (see *Applying* below).

## The model

One row of `stores` (already live) is one venue. Terminals ring sales on
the venue node (PostgreSQL on port 5433); the Python sync engine pushes
completed sales here **with the service role** on reconnect. Dashboards
read through RLS. The cloud is a mirror, never the till.

| Table | Role |
|-------|------|
| `pos_venue_members` | Which auth users may READ which venue. Written by the service role at onboarding. |
| `pos_terminals` | Till registry per venue; sync upserts by `(store_id, terminal_code)`. |
| `pos_business_days` | One per venue per trading date. `UNIQUE (store_id, trading_date)` is the DB backstop for the check-then-insert race the product already hit. |
| `pos_products` | Catalog mirror; `price_cents` VAT-inclusive integer cents. |
| `pos_sales` | The core row. **`UNIQUE (terminal_id, sale_ref)` is the load-bearing idempotency anchor**: sync inserts with `ON CONFLICT DO NOTHING`, so re-pushing a batch is a safe no-op. |
| `pos_sale_lines` | Line snapshots (sku/description copied at sale time); `UNIQUE (sale_id, line_no)`. `qty` is numeric for liquor tots/pours. |
| `pos_sale_payments` | Settlements, split payments as rows; cash carries tendered/change for cash-up. `UNIQUE (sale_id, payment_no)`. |
| `pos_sync_runs` | Audit of sync batches, for duplicate/lost-sale forensics. |

## Decisions worth knowing

- **Money**: integer cents, ZAR, never float. `vat_cents` stores the split
  as the node computed it (15 percent inclusive); the cloud bounds it
  (sign-aware, never exceeding the total) but does not recompute it — node
  rounding is authoritative.
- **Refunds**: negative `total_cents` allowed only when
  `status = 'refunded'`.
- **Cross-venue integrity**: composite FKs (`(terminal_id, store_id)` →
  `pos_terminals (id, store_id)`, same for business days) make it
  impossible for a sale to claim venue B while ringing on venue A's till.
- **`business_day_id` is nullable** on purpose. The node's NOT NULL on it
  is runbook gotcha #4; the cloud mirror must accept late-arriving
  business days rather than lose sales. Sync backfills.
- **RLS**: SELECT-only for `authenticated`, scoped by `pos_venue_members`
  via the SECURITY DEFINER helper `pos_is_venue_member(store_id)`. There
  are **no client write policies anywhere** — writes are service-role
  only (non-negotiable rule 6). `anon` gets nothing.
- **POPIA**: no staff or customer personal data in the cloud; staff are
  the opaque `staff_ref`.
- **No FK to `auth.users`**: managed schema; a membership row must never
  block user deletion. `user_id` is plain uuid.
- **text + CHECK instead of enum types**: status/method/role vocabularies
  will grow; ALTERing a CHECK is one additive migration, enums are not.

## Files

| File | Purpose |
|------|---------|
| `migrations/0001_pos_trading.sql` | The trading schema as an additive, idempotent migration (transactional; fails loudly on the wrong DB). |
| `migrations/0001_pos_trading.down.sql` | Rollback: drops every `pos_*` object (and all mirrored trading data); never touches the registry tables. |
| `tests/behavior.sql` | Behavioural assertions: idempotent re-sync, duplicate rejection, business-day race, money rules, cross-venue FKs, updated_at, and RLS member/non-member/write-refusal. |
| `tests/run_cloud_db_tests.sh` | Disposable PG cluster: stubs what Supabase provides (`stores`, `auth.uid()`, `authenticated`), then verifies apply, double-apply, behaviour, rollback, and rollback idempotency. Exits 0 pass, 77 if no PG server binaries. |

## Testing

```bash
bash db/cloud/tests/run_cloud_db_tests.sh
```

Never touches a real database. Requires PostgreSQL server binaries
(`initdb`, `pg_ctl`); runs the cluster as an unprivileged user when
invoked as root.

## Applying (deliberate step, not part of this change)

1. Run the test harness above; it must print `ALL PASSED`.
2. Restore the Supabase project if paused.
3. Apply as a tracked migration (pick one):
   - Supabase CLI: place the file under `supabase/migrations/` as
     `<timestamp>_pos_trading.sql` and `supabase db push`;
   - SQL editor / psql: run `migrations/0001_pos_trading.sql` verbatim;
   - Supabase MCP `apply_migration` with the file body (records it in
     the migration table).
4. Rollback if needed: run `migrations/0001_pos_trading.down.sql`
   (destroys mirrored trading data; nodes remain source of truth).

## Open questions for Mr LSG

- The sync engine (`SYNC_ENGINE.py`) must target these table and column
  names, or this migration should be adjusted to the engine's existing
  contract before first apply — reconcile whichever way is cheaper.
- Once a venue node's schema is dumped (port 5433), diff its sales/lines/
  payments shapes against this mirror and reconcile field by field.
- Membership provisioning: who writes `pos_venue_members` rows at
  onboarding (billing app? admin tool?), and does the existing
  registry-table RLS (`using (true)` reads for any authenticated user)
  need tightening to the same venue scoping?
