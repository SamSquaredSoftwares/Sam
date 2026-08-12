# SAMePOS monetization model

Analysis and design for how SAMePOS generates income through usage, and the
database core (`db/migrations/025_monetization.sql`) that implements it.

## The software being monetized

SAMePOS is Sam Squared's on-site point-of-sale node: a Windows PC at a venue
(bar, club, restaurant, liquor store) running the POS server with an embedded
PostgreSQL database and a FastAPI app. Each node is one venue box, identified
by a machine-bound **install code** (licensing core, `024_licensing.sql`):
14-day standard trial on first boot, then Ed25519-signed licences bound to the
install code.

The licensing core answers *"may this node trade?"*. It does not, by itself,
generate revenue. This model adds the two revenue streams and the hardware
lock that protects them.

## Revenue model at a glance

| Stream | Trigger | Amount | When |
|--------|---------|--------|------|
| **Usage fee** | Client generates + uploads sales receipts | **10c per transactional row** (configurable: `billing_config.receipt_row_fee_cents`) | Metered per upload, invoiced periodically |
| **Once-off service charge** | Licensing & integration of the software | **To be confirmed** per client (quote → confirm) | Once per node, on activation |
| **Machine binding** | Confirming the once-off charge | — (protection, not revenue) | Product locks to that machine id permanently; not transferable |

Money is stored in integer minor units (cents) — never floats. Default
currency is **ZAR** (`billing_config.currency`, configurable).

### 1. Usage: 10c per transactional row on receipt upload

Every time the node generates sales receipts and uploads them, the FastAPI app
records the batch:

```sql
SELECT * FROM billing_record_receipt_upload(
    :batch_ref,      -- upload's idempotency key (e.g. a UUID per upload)
    :row_count,      -- transactional rows in the batch  <- the billable unit
    :receipt_count,  -- receipts in the batch (informational)
    :machine_id,     -- this machine's hardware fingerprint
    :detail);        -- jsonb: file name, receipt range, ...
```

The charge is `row_count × receipt_row_fee_cents`, snapshotted at charge time:

- **Idempotent.** Replaying the same `batch_ref` (retry after a network blip)
  returns the original charge — the client can never be double-billed for one
  upload. Idempotency is arbitrated by the unique index, not a read-then-write,
  so two simultaneous replays cannot both insert. The same `batch_ref` with a
  *different* row count is rejected as a conflict.
- **Rate-snapshotted.** `rate_cents` is copied onto the batch and
  `amount_cents` is a generated column (`rows × rate`), so recorded charges
  never drift when the price changes later.
- **Machine-enforced.** Once the product is bound, uploads from any other
  machine (or with no machine id) are rejected and audited.
- **Audited.** Every charge and every rejection lands in `billing_audit`.

Worked examples at the default 10c/row:

| Venue profile | Rows/day | Usage revenue/month (±30 days) |
|---------------|---------:|------------------------------:|
| Small liquor store (~120 receipts × 3 rows) | 360 | R 1 080 |
| Busy bar (~400 receipts × 4 rows) | 1 600 | R 4 800 |
| Club weekend trade (~900 receipts × 5 rows) | 4 500 | R 13 500 |

### 2. Once-off licensing & integration service charge (amount TBC)

The fee for licensing + integrating the software is charged once per node.
Because the amount is agreed per client, the flow is quote → confirm:

```sql
-- while the amount is still being agreed (amount may be NULL = TBC):
SELECT * FROM billing_quote_service_charge(NULL, 'licensing + integration');

-- when the client confirms the amount (e.g. R2 500.00):
SELECT * FROM billing_confirm_service_charge(:machine_id, 250000, :confirmed_by, :hostname);
```

Rules enforced by the schema:

- A charge cannot be **confirmed** until an amount exists (quoted or passed at
  confirmation) — "amount not confirmed" is returned, nothing binds.
- At most **one live once-off charge per node** (partial unique index).
- Re-confirming with the same machine is an idempotent success; re-confirming
  with a **different amount** is rejected (no silent re-pricing); re-confirming
  from a **different machine** is rejected.

### 3. Machine binding: bound on confirmation, never transferable

Confirming the once-off charge writes `billing_machine_binding` — the
product's permanent lock to that machine id (the hardware fingerprint the app
computes, e.g. from the Windows MachineGuid + board serial):

- The binding row can **never be updated, deleted or truncated** — triggers
  raise on any attempt (row-level triggers do not fire on `TRUNCATE`, so a
  statement-level `BEFORE TRUNCATE` guard covers that route too, on the
  binding and on every money table). There is deliberately **no unbind
  function**: moving the product to another machine is a vendor-side
  intervention, by design.
- The node's **install code is locked** from then on: `licence_set_node()`
  with a different install code raises, closing the licence re-homing
  loophole.
- **Receipt uploads from any other machine are rejected** (and audited), so a
  copied database on new hardware cannot keep accruing legitimate usage.
- The app should call `SELECT billing_machine_ok(:machine_id)` on boot and
  refuse to run when it returns false.

Before binding (trial / pre-activation), any machine passes the check and
uploads are accepted — the trial experience is unchanged.

## Billing cycle

```
uploads accrue ──► billing_generate_invoice([period])
                        │ rolls up unbilled batches in the period
                        │ + the confirmed once-off charge (once)
                        ▼
                issued invoice (INV-YYYYMM-000001)
                        │  billing_mark_invoice_paid(id, 'EFT-...')
                        ▼
                      paid — once-off charge marked paid with it
```

- Defaults to the **previous calendar month** when called with no arguments
  (run it from a month-end job).
- The sweep is one-sided: **every still-unbilled batch before `period_end`** is
  included, not just those inside the nominal month. A node that was powered
  off over a month end (so the job never ran) has its earlier usage picked up
  by the next run instead of being stranded forever. `period_start` is the
  invoice's label; `period_end` is the real cut-off.
- Batches are stamped with the invoice id **in the same statement that totals
  them**, so an upload landing mid-run is either fully billed or left for next
  time — never stamped-but-unbilled. A batch can never be billed twice, and
  re-invoicing the same period returns "nothing to invoice".
- Invoicing and confirmation take a **per-node advisory lock**, so two
  simultaneous month-end runs cannot both issue an invoice for the same usage,
  and two simultaneous confirmations cannot both bind.
- Invoices are billed in the **currency the charges were recorded in**; a
  ledger with mixed currencies (config changed mid-period with items
  outstanding) raises rather than summing incompatible money.
- **Correcting a bill:** `billing_void_invoice(id, reason)` voids an *issued*
  invoice and releases its batches and once-off charge back to unbilled, so the
  next run re-bills them. Voiding by hand with a bare `UPDATE` would strand
  that revenue permanently — use the function. Paid invoices cannot be voided.
- `billing_status` (view) is the app-facing readout for `/api/billing/status`:
  binding state, once-off charge state, current rate, unbilled accrual, and
  outstanding invoiced balance.

## Enforcement posture

Consistent with the licensing core: the **database is the system-of-record and
enforcement point**; the app owns user-facing behaviour. Hard rules (machine
mismatch, double-billing, binding immutability) are enforced in the schema.
Whether unpaid invoices should *block trading* is a business decision — the
licensing core's `hard_enforcement` guard already exists for licence validity,
and a similar guard keyed on `outstanding_cents` could be added later if
desired. Advisory-first is recommended: a venue mid-service should not go dark
over an unpaid invoice without a deliberate decision.

## App integration checklist (FastAPI)

| Endpoint / job | Backing call |
|----------------|--------------|
| boot check | `SELECT billing_machine_ok(:machine_id)` |
| `POST /api/receipts/upload` | `billing_record_receipt_upload(...)` after storing the upload; return HTTP 402/409 with `reason` when `success=false` |
| `GET /api/billing/status` | `SELECT * FROM billing_status` |
| `POST /api/billing/quote` (vendor) | `billing_quote_service_charge(...)` |
| `POST /api/billing/confirm` (vendor) | `billing_confirm_service_charge(...)` |
| month-end job | `SELECT * FROM billing_generate_invoice()` (run with a stable session TimeZone — the node's trading TZ) |
| payment reconciliation | `billing_mark_invoice_paid(:id, :ref)` |
| correcting a bill | `billing_void_invoice(:id, :reason)` — releases its usage back to unbilled |

The app computes the machine fingerprint; the database never trusts a machine
id it wasn't given — which is why every metering/confirmation call takes
`p_machine_id` and compares it against the binding.

## Open decisions (deliberately configurable / not yet decided)

1. **Once-off amount** — TBC per client; the schema models it as a quote whose
   amount may be NULL until confirmation.
2. **Currency** — defaults to ZAR; set `billing_config.currency` per market.
3. **VAT/tax** — amounts are net; add a tax line at invoice-render time or a
   `vat_bp` config column when the invoicing entity/rate is settled.
4. **Payment rails** — invoices carry a `paid_reference` free-text field (EFT,
   card, debit order); no gateway integration is assumed.
5. **Dunning** — what happens when `outstanding_cents` ages: reminders in the
   app, then optionally a hard guard (see enforcement posture above).
6. **Where receipts are uploaded to** — the metering point is the node's
   upload API regardless of destination (head office, accountant, vendor
   portal).

## Files

| File | Purpose |
|------|---------|
| `db/migrations/025_monetization.sql` | The monetization core as a numbered, idempotent, additive migration (requires 024). |
| `db/migrations/025_monetization.down.sql` | Rollback — drops every billing object (deletes all billing state; run before 024's rollback). |
| `db/schema/monetization.schema.sql` | Same objects as a standalone schema; equivalence enforced by the test harness. |
| `db/tests/behavior_monetization.sql` | Behavioural assertions (27 scenarios: charging, idempotency, TBC quote→confirm flow, binding immutability incl. TRUNCATE, invoicing, void/re-bill, stranded-usage sweep, invoice/ledger reconciliation). |
| `db/tests/run_db_tests.sh` | Shared harness: disposable PG cluster, both cores, equivalence/idempotency/rollback. |
| `tests/test_monetization_db.py` | Pytest wrapper (skips when PG server binaries are absent). |
