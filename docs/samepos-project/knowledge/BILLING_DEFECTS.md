# BILLING_DEFECTS.md - Defects found in billing implementations, and the guards that stop them

STATUS: Seeded 2026-09-12. This is a **pre-implementation checklist**, not a bug
list for code that still exists. Two SAMeSync/SAMePOS billing implementations
were built and adversarially reviewed; 28 real defects were found, several
reproduced live against a real database. The implementations are gone. The
defects are not - they are the defects this problem shape produces, and any new
billing code (Stripe + Prisma + Supabase, or anything else) will produce them
again unless each guard below is present.

Provenance: [review] = found by adversarial review of real code, with a
reproduced or code-traced failure scenario. Stack-independent unless noted.

## Why this file exists

Both implementations passed their own tests. The defects were found by reviewing
*against the money*, not against the spec: three independent reviewers asked
"how does this double-charge, silently lose revenue, or give the product away",
and each found critical defects the author had not. Budget for that review step.

---

## 1. Double-charging

| # | Defect | Guard |
|---|---|---|
| 1 | **One paid order mints unlimited machine-bound licences.** `order_id` had no uniqueness constraint and the check-then-insert raced, so one payment could license an unbounded number of machines. | Enforce in the **database**: unique index on `(order_id, install_code)` plus a seat-budget constraint that re-counts live licences inside the write. A concurrent double-POST must lose at the index, not at an application check. [review] |
| 2 | **A settled payment processed twice** when the provider webhook races the redirect-verify. | One settlement path shared by both routes, keyed on the provider event id with a `processed` flag. This is the pack's own rule: the webhook handler must be idempotent. [pack] |
| 3 | **A re-metered client ledger double-charges.** The client's hash changes when a day is re-metered (new `at`/`run_id`/`prev_hash`), so hash de-duplication does not catch it - the same work arrives under a new key. | De-duplicate on `(install_code, client_seq)` **as well as** entry hash, and treat a ledger position arriving with a different hash as a fork: record it, do not bill it. [review] |
| 4 | **Duplicate entries inside one batch** both pass planning (the de-dupe checked the database, not the batch), then collide on a unique index and abort the whole write. | De-duplicate the batch by idempotency key *before* planning, not only against stored rows. [review] |
| 5 | **Credits consumed twice for one usage entry.** | Tie the consumption row to the usage entry's key with a unique index, so exactly-once is a schema property. [review] |
| 6 | **Re-invoicing a period bills usage already billed.** | Stamp each usage row with the invoice/statement that claimed it, and select only unclaimed rows. [review] |

## 2. Silent revenue loss

| # | Defect | Guard |
|---|---|---|
| 7 | **`acknowledged_through` echoed the client's own `ledger_to`** instead of what was actually stored. The client advances its watermark on any 2xx and never re-sends, so a short batch dropped billable usage permanently. | A 2xx must mean **durably stored**. Acknowledge only what was written. The empty-batch early return is the same bug in miniature. [review] |
| 8 | **Rejected entries acknowledged by a 2xx with no durable record.** | Anything that cannot be stored either fails the batch (5xx, client re-sends) or lands in a durable quarantine row a human can act on. Never silently dropped, never silently billed. Read the quarantine row back and fail if it did not land. [review] |
| 9 | **A two-sided period window stranded usage forever.** A node powered off over a month end had that month's usage fall outside every later window. | Sweep **all** still-unbilled usage before the period end, one-sided. `period_start` is a label; `period_end` is the cut-off. [review] |
| 10 | **Usage claimed onto a draft nothing could issue or void**, making it permanently uncollectable while the dashboard showed R0 unbilled. | Every state a row can be claimed into needs a route that moves it forward *and* a route that releases it. Model void as releasing its rows back to unbilled. [review] |
| 11 | **A batch committed between the SUM and the stamping UPDATE** was stamped but not counted - billed nowhere, invisible in every readout. | Stamp and total in **one** statement (`UPDATE ... RETURNING`). [review] |
| 12 | **A zero-amount (comped) charge was never attached to an invoice** and could never reach paid. | Gate on existence, not on amount. [review] |
| 13 | **One unstorable entry froze the install's sequence watermark forever**, disabling chain and history checks from then on. | Advance the watermark from what was stored; never let one poisoned entry wedge the stream. [review] |
| 14 | **A single poisoned batch wedged a venue's reporting permanently** (503 on every retry, watermark never advances, that machine never reports again). | Any error the client will retry identically must be either transient or quarantined. An infinite-retry path is a permanent outage. [review] |

## 3. Trusting the client

| # | Defect | Guard |
|---|---|---|
| 15 | **Invoice lines billed a client-supplied amount the server had already proved wrong.** The server recomputed `quantity x rate`, flagged the mismatch, stored the flag - and then billed the client's number anyway. | Recompute money at billing time from quantity x rate; never sum a client-supplied amount. Exclude flagged rows from billing entirely. [review] |
| 16 | **No bound on per-entry quantity.** One entry with `quantity_billed: 10000000` drained a prepaid balance to its floor - and, once enforcement was on, denied the venue its own reporting. | Bound per-entry and per-batch quantities to something a real venue can produce, and reject beyond it. [review] |
| 17 | **A client-supplied `sku` bypassed the plan-to-product binding**, so a chosen plan could be charged at another product's price. | Derive price server-side from the live price table; never let a client identifier select it. [review] |
| 18 | **A provider-reported amount was accepted without comparison.** | Compare against the stored expected amount and refuse a mismatch. Never mark success without provider confirmation. [review] |

## 4. Machine binding and licensing

| # | Defect | Guard |
|---|---|---|
| 19 | **`TRUNCATE` erased the "permanent" machine binding.** Row-level triggers do not fire on TRUNCATE, so an UPDATE/DELETE guard alone left a one-statement bypass. | Add `BEFORE TRUNCATE ... FOR EACH STATEMENT` guards on the binding and on every money table. [review] |
| 20 | **Install code re-homing** let a licence follow a different machine. | Lock the install code once bound, on INSERT as well as UPDATE (so delete-and-reinsert is also blocked). [review] |
| 21 | **A multi-seat order charged N x the fee but could only ever yield one key.** | Make seats real: N seats permit exactly N distinct install codes, no more. [review] |
| 22 | **A race-recovery path returned a licence key that existed in no row.** | Never return a key that is not durably stored. [review] |
| 23 | **Cancelling an invoiced charge re-opened the one-live-charge slot**, permitting a second charge for the same node. | Refuse the transition while the charge is attached to a live invoice. [review] |

## 5. Auth and abuse

| # | Defect | Guard |
|---|---|---|
| 24 | **The usage endpoint was unauthenticated**, so anyone holding an install code - printed on the till, and carried in plaintext inside the licence key - could write billable rows against another venue. Under prepaid credits this drains their balance. | Authenticate the install with a bearer credential and make it **required by default**. A permissive default is the hole. [review] |
| 25 | **Session cookie without the `__Host-` prefix**, so any sibling subdomain could fix a victim's session - and a real sibling subdomain existed. | `__Host-` prefix, Secure, HttpOnly, SameSite. [review] |
| 26 | **An unauthenticated caller could overwrite another account's invoice legal name and VAT number.** | Authorize every write against the signed-in account. [review] |
| 27 | **Magic-link and session tokens must be stored as hashes only**, single-use, expiring, and rate-limited per email and IP; the request endpoint must not enumerate accounts. | [review] |

## 6. Currency and rounding

| # | Defect | Guard |
|---|---|---|
| 28 | **Mixed currencies summed into one total** when the configured currency changed mid-period with items outstanding. | Bill in the currency the charges were recorded in; refuse to sum across currencies. Money is integer cents, never float [pack]. And VAT: store the 15%-inclusive split (net + vat = total), never recompute later from a rate that may have changed [pack]. |

---

## How to use this file

Before billing code is considered done, walk the table and point at the guard
for each row - in the schema where the guard is a constraint, in the code where
it is logic, and in a test where it is behaviour. A comment claiming a fix is
not a fix; two of these defects survived a first repair round because the guard
was structurally inoperative for exactly the units that cost money.
