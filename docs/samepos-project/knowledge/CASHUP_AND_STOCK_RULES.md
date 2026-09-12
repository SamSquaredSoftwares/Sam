# CASHUP_AND_STOCK_RULES.md
# SAMePOS: Cash-Up, Stock, VAT, Sync, and Licensing Rules

STATUS: DRAFT. Built on 2026-08-25 from the Sam repo plus Mr LSG's setup pack.
The real cash-up and stock logic lives in the OLD Claude Project chats and is
NOT in this file yet. To finish this file: run the rescue prompt below in the
old project, then merge its output into every section marked "TO FILL".
**Until that merge happens, treat every blank in this file as a live risk.**

Provenance labels used throughout:

| Label | Meaning |
|-------|---------|
| [repo] | Verified in a file in the Sam repo (db/README.md, db/schema/licensing.schema.sql, and others) |
| [pack] | Stated by Mr LSG in the setup pack. Authoritative user context |
| [draft] | Inference or industry-standard assumption. Mr LSG must confirm |

---

## THE RESCUE PROMPT (run this in the OLD project, merge the output here)

```
Export everything you know about SAMePOS that is NOT already in a code file.
Cover: cash-up rules and edge cases, stock control logic, loss event rules,
VAT handling, sync conflict rules, and any provider config decisions.
Output as one markdown document. Use placeholders like <SUPABASE_SERVICE_KEY>
for every secret. Do not print real keys.
```

Save the output. Paste it into the matching sections below. Keep the
provenance labels. Anything from the old chats gets a new label: [old-project].

---

## 1. PRODUCT CONTEXT (so this file stands alone)

- SAMePOS is an offline-first point of sale for South African retail and
  hospitality, built by Sam Squared Softwares (Pty) Ltd. [pack]
- First live venue is Booth Liquor. [pack]
- Staff use cheap Windows terminals with bad internet. Offline is the normal
  state, not the error state. [pack]
- Architecture: offline-first event sourcing with immediate sync on reconnect.
  Cloud layer is Supabase (Postgres, Auth, RLS, Realtime, Edge Functions).
  Local cache and fallback is PostgreSQL 16 on port 5433 with CDC triggers.
  Sync engine is Python, roughly 900 lines. Dashboard is React and TypeScript
  with a dual connection (cloud plus local). [pack]
- The node also runs a FastAPI app that serves the POS API (for example
  POST /api/orders and the licence endpoints) against an embedded
  PostgreSQL. [repo]
- The node replaces or migrates from the legacy DigitotPOS system. Node
  commissioning uses configure-node.ps1 and deploy-update.ps1, and an install
  is not done until a real sale rings up end-to-end and licensing is
  active. [repo]
- TENSION TO RECONCILE: the repo also holds a GitHub Actions workflow
  configured to build an ASP.NET Core app named "SAMePOS" and deploy it to an
  Azure App Service (Production slot); the repo itself holds no .NET source,
  so it cannot succeed as committed. [repo] The pack describes the cloud layer as
  Supabase and says nothing about Azure or .NET. [pack] Both are real. Mr LSG
  must state what the Azure app is (legacy? billing? licensing server?
  dashboard host?) and whether it is still current.
- Currency is ZAR. VAT is 15 percent, inclusive by default. Timezone is
  Africa/Johannesburg. [pack]

---

## 2. MONEY RULES (apply to everything below)

1. Money is never a float. Store cents as integers. [pack]
2. Display ZAR with 2 decimals. [pack]
3. VAT is 15 percent and inclusive by default. Show the VAT split on every
   money calculation. [pack]
4. Sync is idempotent. The unique constraint on (terminal_id, sale_ref) is
   load bearing. Never break re-sync safety. [pack]
5. Customer fields are POPIA scoped. Collect the minimum. Never log personal
   data in plain text. [pack]

---

## 3. CASH-UP RULES AND EDGE CASES

**Cash-up is where staff steal and where trust dies.** [pack]

### 3.1 What is known now

Known edge-case families that can produce a wrong cash-up total: [pack]

| Edge case family | Why it is dangerous |
|------------------|---------------------|
| Multi-staff shifts | More than one person handles the same drawer |
| Mid-shift staff swaps | Responsibility changes hands mid-count |
| Offline voids | A void made offline must survive sync without double-counting |
| Split payments | One sale, several tenders; the parts must sum exactly |
| Refunds | Negative money moving against an earlier sale, maybe on a later day |

Repo-verified facts that shape cash-up:

- The product has a business day concept. Sales orders carry a
  business_day_id, and it is NOT NULL: gotcha #4 in the node runbook is a
  NOT NULL violation on sales_order.business_day_id at POST /api/orders.
  Opening a tab fails if no business day is open. [repo]
- The product hit a duplicate-creation race on business_day: concurrent
  callers each passed a check-then-insert and created duplicate business
  days. It was fixed with an advisory lock. [repo] A duplicate business day
  splits one trading day's sales across two buckets, which corrupts the
  cash-up for that day. [draft]
- Sales orders are created when staff open a tab, at POST /api/orders, and
  payments are recorded in a separate payment table. [repo] The exact payment
  table name is not pinned down; the repo probes sales_payment, sale_payment,
  order_payment, then payment. [repo]

### 3.2 Draft working rules (Mr LSG must confirm every one)

- [draft] A cash-up reconciles counted drawer cash against expected cash:
  opening float, plus cash sales, minus cash refunds and paid-outs, for one
  business day (or one shift) on one terminal.
- [draft] Expected totals come from the local PostgreSQL, not the cloud, so
  cash-up works fully offline.
- [draft] A cash-up is per staff member per shift, not per terminal, because
  multi-staff shifts and mid-shift swaps are named edge cases. Confirm which.
- [draft] A void made after a cash-up is closed must not silently change the
  closed total. It should land in the next period or raise an exception
  record. Confirm the actual rule.

### 3.3 TO FILL from the old project

- The exact cash-up procedure, step by step. Who counts, who witnesses, who
  signs off.
- Whether cash-up is per shift, per business day, per terminal, or per staff
  member. What closes what.
- Opening float rules: fixed amount? carried over? who sets it?
- How a mid-shift staff swap is recorded. Is there a drawer handover count?
- How offline voids are authorized (PIN? manager card?) and how they appear
  in the cash-up.
- Split payment rules: allowed tender combinations, rounding of the split,
  which part carries the VAT display.
- Refund rules: same-day vs later-day, cash refund of a card sale allowed or
  not, manager authorization, effect on the day's expected cash.
- Variance rules: what over/short is tolerated, what triggers escalation,
  where variance is recorded.
- Tips, paid-outs, petty cash, and cash drops to a safe: do these exist, and
  how do they hit the expected total?
- The exact tables and columns cash-up reads and writes. (SCHEMA.sql,
  knowledge file #2, is the check on any name Claude uses. Do not trust a
  table or column name that is not in SCHEMA.sql.)

---

## 4. STOCK CONTROL LOGIC

Almost nothing about stock control is verifiable from this repo. The node
imports a catalog from DigitotPOS at commissioning [repo], and that is the
only stock-adjacent fact on record here. Everything else must come from the
old project. Answer each question below in place.

### 4.1 Receiving

- How is stock received? Against a supplier invoice, a purchase order, or
  free-form entry?
  ANSWER: (TO FILL)
- The pack says AI supplier invoice scanning runs in a Deno/TypeScript Edge
  Function calling Claude. [pack] What does the scan output, and does it
  create stock movements directly or a draft a human confirms?
  ANSWER: (TO FILL)
- Cost price handling: last cost, average cost, or FIFO?
  ANSWER: (TO FILL)

### 4.2 Counts

- Full stock take vs cycle counts: which, how often, and who does them?
  ANSWER: (TO FILL)
- Can the venue trade during a count? How are sales during a count handled?
  ANSWER: (TO FILL)
- Is a count offline-safe, and how does it sync?
  ANSWER: (TO FILL)

### 4.3 Variance

- How is variance computed (counted minus expected)? At what level: item,
  category, or total value?
  ANSWER: (TO FILL)
- What variance is acceptable before escalation? Is there a per-item
  tolerance for liquor?
  ANSWER: (TO FILL)
- Who signs off a variance, and is the adjustment a stock movement with an
  audit trail?
  ANSWER: (TO FILL)

### 4.4 Tot and pour measures (liquor)

Booth Liquor is the live venue, so this section matters most. [pack]

- How are tots defined? Standard SA tot is 25 ml [draft]; does the product
  use 25 ml, venue-configurable measures, or per-product measures?
  ANSWER: (TO FILL)
- How does selling a tot deplete bottle stock? Fractional units, a
  bottle-to-tot conversion factor, or a separate tot stock unit?
  ANSWER: (TO FILL)
- How is a partially used open bottle counted in a stock take? Tenths of a
  bottle, weight, or estimate?
  ANSWER: (TO FILL)
- Expected pours per bottle: is spillage factored in (for example 750 ml
  bottle at 25 ml tot = 30 pours theoretical [draft])? What is the accepted
  yield?
  ANSWER: (TO FILL)

### 4.5 Wastage

- How are breakage, spillage, spoilage, and free pours recorded? Are they
  distinct reason codes?
  ANSWER: (TO FILL)
- Does wastage require manager authorization? Is there a daily wastage cap
  or report?
  ANSWER: (TO FILL)

---

## 5. LOSS EVENT RULES

Unknown in this repo. The term "loss event" comes from the pack's rescue
prompt scope. [pack] Answer in place.

- What counts as a loss event? Theft, breakage, unexplained variance, void
  abuse, refund abuse, drawer short?
  ANSWER: (TO FILL)
- Is a loss event a record in its own table, or a classification on another
  record (stock movement, cash-up variance)?
  ANSWER: (TO FILL)
- Who can create one, who must approve it, and can it be edited after
  creation? [draft] Expect append-only with a reversal, matching the
  event-sourcing architecture. Confirm.
  ANSWER: (TO FILL)
- Do loss events feed a report or alert (for example on the dashboard)?
  ANSWER: (TO FILL)
- Do loss events affect the cash-up expected total, the stock expected
  quantity, both, or neither?
  ANSWER: (TO FILL)

---

## 6. VAT HANDLING

### 6.1 Pinned down by the pack

- VAT is 15 percent. [pack]
- Prices are VAT inclusive by default. [pack]
- The VAT split must be shown on every money calculation. [pack]
- All amounts are cents as integers. [pack]

### 6.2 Standard SA VAT notes (all [draft], confirm before relying on them)

- [draft] SA retail prices are quoted VAT inclusive by law for consumers,
  which matches the pack's "inclusive by default".
- [draft] Extracting VAT from an inclusive price: vat_cents =
  round(total_cents * 15 / 115). Net = total_cents - vat_cents. Confirm the
  rounding rule the product actually uses (per line vs per total makes real
  cent differences on split payments).
- [draft] Some items can be zero-rated (basic foodstuffs) or exempt. A liquor
  venue mostly sells standard-rated goods, but the product may still need a
  per-item VAT category. Confirm whether one exists.
- [draft] A valid SA tax invoice above R5000 needs the buyer's details; under
  R5000 an abridged tax invoice is enough. Confirm what the receipt prints
  and whether the product issues full tax invoices on request.
- [draft] Refunds and voids need matching VAT reversal (credit note logic).
  Confirm how the product records these.

### 6.3 TO FILL from the old project

- The exact VAT rounding rule, and where it is applied (line, order, or
  payment level).
- Per-item VAT categories: do they exist, and what are the values?
- Credit note / refund VAT handling.
- Whether any VAT reporting or period totals are produced for the venue.

---

## 7. SYNC CONFLICT RULES

**The source of truth is SYNC_ENGINE.py** (knowledge file #4, roughly 900
lines, with conflict resolution). [pack] This section does not restate the
code. It lists what the old chats and the code must jointly answer.

Known constraints:

- Offline-first event sourcing with immediate sync on reconnect. [pack]
- Local PostgreSQL 16 on port 5433 with CDC triggers feeding the sync
  engine. [pack]
- Idempotency rests on the unique constraint on (terminal_id, sale_ref).
  Re-syncing the same sale must be a no-op, never a duplicate. [pack]

Questions the old chats should answer (merge answers here):

1. Conflict policy per entity: last-write-wins, cloud-wins, node-wins, or
   merge? Which entities differ (sales vs catalog vs staff)?
   ANSWER: (TO FILL)
2. Direction of authority: is the node authoritative for sales and the cloud
   authoritative for catalog and pricing? [draft] That split is the common
   pattern; confirm.
   ANSWER: (TO FILL)
3. Clock handling: are terminal clocks trusted? Is there drift protection
   before timestamps decide a conflict?
   ANSWER: (TO FILL)
4. What happens to a sale edited (voided, refunded) offline on one terminal
   while the same sale already synced from another path?
   ANSWER: (TO FILL)
5. Poison messages: where does a row that fails sync repeatedly go? Dead
   letter table, retry cap, alert?
   ANSWER: (TO FILL)
6. Ordering: does sync preserve event order per terminal, and does anything
   break if events land out of order?
   ANSWER: (TO FILL)
7. Business day boundaries: how does a sale that syncs after its business day
   closed get bucketed? (This interacts directly with cash-up, section 3.)
   ANSWER: (TO FILL)

---

## 8. LICENSING AND TRIAL RULES

This section IS fully known. It is verified against db/README.md and
db/schema/licensing.schema.sql in the Sam repo. All facts below are [repo]
unless marked otherwise.

### 8.1 The model

- A SAMePOS node is one venue box, identified by a machine-bound install
  code (stored as a singleton in licence_node).
- Standard trial: 14 days by default, auto-started on first boot via
  licence_ensure_trial(). Length is tunable in licence_config
  (standard_trial_days) or via the product's SAMEPOS_TRIAL_STANDARD_DAYS
  env var passed as licence_ensure_trial(days).
- Signed licenses: Ed25519 keys with the "SPOS1." prefix, bound to the
  node's install code. The private signing key lives only in the vendor
  keystore.
- Activation: POST /api/licence/activate. The app (FastAPI on the node)
  verifies the Ed25519 signature with the vendor public key. PostgreSQL
  cannot verify Ed25519 with stock functions, so crypto stays in the app.
  The app then calls licence_record_signed(...), and the database records
  the result. The database is the system-of-record and enforcement point,
  not the verifier.
- licence_record_signed enforces the install-code binding and the verified
  flag. Business rejections (unverified signature, install-code mismatch)
  return success=false with a reason instead of raising, so the reject
  audit row commits. A licence row is written only on success.
- Status: GET /api/licence/status reads the licence_status view, which
  computes the governing license. States: trial, extended_trial, licensed,
  grace, expired, none. Precedence: valid licensed > valid extended_trial >
  valid trial. A NULL expires_at means perpetual.
- Every lifecycle event (trial_start, activate, revoke, reject) lands in an
  append-only licence_audit table.

### 8.2 Enforcement: advisory by default, optional hard guard

- Default posture is advisory. The app reads licence_status and decides
  what to do (refuse to open a tab, show a banner). The recommended posture.
- An optional hard guard exists: a BEFORE INSERT trigger
  (licence_guard_sales) on the payment table that blocks payment inserts
  when the license is invalid. It is gated by
  licence_config.hard_enforcement, which ships false, so it does nothing
  until an admin turns it on:

```sql
-- turn hard blocking on / off
UPDATE licence_config SET hard_enforcement = true;
UPDATE licence_config SET hard_enforcement = false;
```

- Why payments and not tab-opens: sales_order rows are created when staff
  open a tab (runbook gotcha #4 is the NOT NULL violation on
  sales_order.business_day_id at POST /api/orders). Guarding that insert
  would stop staff opening or continuing tabs and could strand an open
  shift mid-service. Guarding the payment insert lets an in-progress shift
  keep serving while preventing the node from taking money on a dead
  license.
- The real payment table name is not pinned down. The migration probes
  sales_payment, sale_payment, order_payment, payment and attaches to the
  first that exists. If the product uses a different name, attach
  explicitly: SELECT licence_attach_guard('public.<payment_table>');

### 8.3 Trial-race protection

- On first boot, several tablets can call licence_ensure_trial() in the
  same second. A bare check-then-insert would create duplicate trials, the
  same race the product hit on business_day.
- Protection is two layers: licence_ensure_trial() takes
  pg_advisory_xact_lock(hashtext(install_code)) and re-reads under the
  lock, and a partial unique index (licence_one_auto_trial_uidx: one row
  per install_code where mode = 'trial' and source = 'auto') is the
  database-level backstop.

### 8.4 One open caveat

- The licensing core (migration 024) was authored from the documented model
  and has NOT been diffed against the product's own migrations 001 to 023,
  and has not been run on a real node. db/tools/check_reconciliation.sql is
  the read-only pre-flight to run on a real node first. [repo]

---

## 9. GAPS TO FILL FROM THE OLD PROJECT

Work through this list. Check each item off as its answer is merged in above.

1. [ ] Run the rescue prompt (top of this file) in the old project. Save the
   raw output somewhere safe before editing anything.
2. [ ] Cash-up: full procedure, period definition (shift/day/terminal/staff),
   float rules, handover counts, variance tolerance and escalation.
3. [ ] Cash-up: offline void authorization and how voids and refunds hit a
   closed vs open cash-up.
4. [ ] Cash-up: split payment rules and rounding.
5. [ ] Cash-up: tips, paid-outs, cash drops, petty cash.
6. [ ] Stock: receiving flow and what the AI invoice scan produces.
7. [ ] Stock: count procedure, trading-during-count rule, offline behavior.
8. [ ] Stock: variance computation, tolerances, sign-off.
9. [ ] Liquor: tot measure definition, bottle depletion math, open-bottle
   counting, accepted yield.
10. [ ] Wastage: reason codes, authorization, reporting.
11. [ ] Loss events: definition, storage, approval, downstream effects.
12. [ ] VAT: exact rounding rule and level, item VAT categories, credit note
    handling, receipt/tax invoice content.
13. [ ] Sync: per-entity conflict policy, authority split, clock handling,
    dead-letter path, ordering guarantees, late-sale business day bucketing.
14. [ ] Provider config decisions (Supabase, Stripe, others) with
    placeholders only. Never a real key.
15. [ ] Reconcile the Azure App Service ASP.NET Core "SAMePOS" deploy with
    the Supabase architecture. What is that app, and is it current?
16. [ ] Confirm or correct every [draft] line in this file, then relabel it.

**The single next action: run the rescue prompt in the old project. Nothing
else in this file matters as much.**
