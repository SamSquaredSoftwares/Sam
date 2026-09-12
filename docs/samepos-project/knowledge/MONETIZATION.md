# MONETIZATION.md - How SAMePOS and SAMeSync earn

STATUS: Seeded draft, written 2026-09-12. Records the commercial model and the
billing decisions behind it, so they are not re-derived in chat. To finish:
1) answer the open decisions at the bottom, 2) confirm every [draft] why,
3) add a row to DECISIONS.md for each decision confirmed here.

Provenance tags match DECISIONS.md: [pack] stated by Mr LSG (authoritative),
[repo] verified in a file in this repo, [draft] inferred - must be confirmed.

## Two products, sold two different ways

| | SAMePOS | SAMeSync |
|---|---|---|
| What | Offline-first POS for SA retail/hospitality | Daily POS -> QuickBooks Online bookkeeping |
| Sold as | Recurring monthly/annual licence, hardware leased, **quote-based** - the site deliberately lists no prices | **Self-serve purchase**: one-off software fee + prepaid credits |
| Licensing | Ed25519 `SPOS1.` keys bound to a machine install code [repo] | Machine install code + licence key via `samesync.licensing.gate` [repo] |
| Provenance | [pack] + [repo] | [pack] |

SAMeSync is the first self-serve purchasable product. Any storefront work must
introduce "buy now" **without** undermining SAMePOS's quote-based positioning -
the pricing page's own FAQ explains why prices are not listed.

## SAMeSync commercial model (prepaid credits)

Mr LSG, verbatim: *"There is a one-off fee for the software, which gives you
access to, let's say, 500 credits (that is, 500 line transactions) to be
uploaded into QuickBooks. If they are using our software to AI-upload images and
voices into QuickBooks, that will be charged at a separate rate, possibly around
250 per upload. Receipt uploads will be charged at, let's say, 50 cents per
transaction uploaded."* [pack]

Design decisions taken from that:

| Decision | Why |
|---|---|
| **One currency: credits.** Everything is priced in credits so the customer sees a single balance. | Credits plus separate rand charges gives the customer two mental balances and two things to dispute. [draft] |
| 1 credit = 1 line transaction uploaded to QuickBooks | Mr LSG's own definition. [pack] |
| AI upload = a configurable number of credits (default 5, i.e. R2.50 at 50c/credit) | Keeps the single balance while matching the separate rate. [draft] |
| One-off purchase grants a configurable bundle (default 500 credits) | [pack] |
| Top-ups sold at a configurable cents-per-credit (default 50c) | [pack] |
| **Prepaid, not invoiced.** | Paid before the work is delivered: no debtor book, no chasing a venue for R160, and "balance empty" enforces itself where "unpaid invoice" does not. [draft] |
| **Overdraft, not a hard stop.** Balance may go negative to a configurable floor (default 200 credits); warn from 20% remaining; refuse only past the floor, with an actionable status. | A venue must never have its books silently stop mid-run. Mirrors the licensing rule that a lapsed licence never disrupts a live trading session. [repo] |
| The credit ledger is **append-only and hash-chained**; the balance is derived from it, never a mutable counter. | A counter drifts and cannot be audited. Same rule as the licensing audit trail. [repo] |
| Credit expiry is modelled (`expires_at` on a grant) but defaults to none | Credits that never expire sit as deferred revenue forever; 24 months is the usual compromise. **Undecided.** [draft] |

### The unit problem - read this before pricing anything

A "line transaction" is ambiguous, and the two readings differ by ~100x:

| Credit = | Volume (busy bar) | 500 credits lasts | Revenue/venue/month @ 50c |
|---|---:|---:|---:|
| **QuickBooks import line** (daily summary split by department) | ~10/day | ~50 days | **~R150** |
| **Source POS transaction row** | ~1 600/day | **~7 hours** | ~R24 000 (unsellable) |

Only the import-line reading makes 50c coherent, and it makes the 500-credit
bundle mean roughly 1-2 months. The consequence to accept deliberately:
import-lines at 50c is a **volume-of-venues** business (~R150-300/venue/month),
not a revenue-per-venue one. An earlier model of 10c per *source* row would have
earned ~R4 800/venue/month. Both work; they are different businesses. [draft]

**Load-bearing implementation fact:** the already-built client meter records the
unit `pos_row` (source rows). Charging credits needs a `qbo_line` unit fed by
the QuickBooks import line count, which is available at the same point in the
pipeline. Keep `pos_row` recorded at zero cost for analytics. Do **not** charge
one as if it were the other. [repo]

### VAT

VAT is 15 percent and **inclusive** by default, and every money calculation
stores the split (net + vat = total) rather than recomputing it later from a rate
that might change. [pack] A credit price of 50c is therefore VAT-inclusive
unless Mr LSG says otherwise, and the stored grant/consumption rows must carry
the split. **An earlier billing implementation treated amounts as net and would
have been wrong.**

## Machine binding

SAMeSync is licensed per machine. The venue reads its install code off the
machine (`python -m samesync.licensing.gate install-code`), the vendor issues a
key against it, and the app exits 3 when unlicensed. [repo] Binding rules the
billing side must honour:

- one live licence per install code, and a licence bound to one machine is
  **non-transferable** - moving it is a vendor-side intervention by design;
- a paid order may mint at most as many licences as it has seats, enforced by a
  database constraint, not an application check (see BILLING_DEFECTS.md #1);
- the once-off fee is confirmed vendor-side at key issuance, where the customer
  cannot edit the record.

## The client metering module (built, tested, stack-independent)

`samesync/billing/` - Python, stdlib only, 29 passing tests. It prices and
records billable work locally and reports it upstream:

- **delta-based per period**: re-running an unchanged business day charges
  nothing; a day that grew by 40 late captures charges 40, not the whole day
  again. Reconciliation re-runs are normal, so this is essential.
- **idempotent on `batch_ref`**, arbitrated by a unique index rather than a
  read-then-write, so concurrent replays cannot both insert.
- **rate snapshotted** onto each entry, and the amount is a generated column, so
  a later price change never rewrites history.
- **hash-chained ledger** with tamper detection; metering refuses to append to a
  ledger that fails verification.
- reports to `POST {control_url}/api/usage` with `{install_code, ledger_from,
  ledger_to, entries[]}`, **at-least-once**: any 2xx acknowledges up to
  `ledger_to` and those entries are never re-sent, a 5xx re-sends safely.
  The server must de-duplicate on the client entry `hash`.

Anything server-side must adapt to this contract; the client is deployed.

## Open decisions

1. **"250 per upload" - 250 cents (R2.50) or R250?** A 100x difference. Encoded
   as 250 cents so far, because the AI invoice scan was separately set at R2.50
   per document.
2. **Voice uploads** - "images and voices into QuickBooks". Is voice a real
   planned feature? Audio transcription prices differently from page scans and
   needs its own credit cost.
3. **The one-off software fee amount.** Never set. Every implementation so far
   renders it from a single named constant marked owner-unconfirmed.
4. **Credit expiry** - none, or 24 months?
5. **Payment provider.** [pack] says Stripe (with Prisma + Supabase on Next.js
   App Router). Mr LSG separately chose Paystack for a Cloudflare-hosted
   storefront that mirrored the Cothenticity platform. These conflict - see
   tension 3 in DECISIONS.md.
6. **Per-document vs per-page AI scan pricing.** Per document is simpler and is
   what was chosen; a 40-page consolidated supplier statement then costs the
   same as a one-page delivery note while costing more to scan. Page counts are
   recorded either way, so the mix can be watched before switching.
