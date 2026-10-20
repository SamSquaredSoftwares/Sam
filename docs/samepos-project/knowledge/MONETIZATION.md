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
| **Overdraft, not a hard stop.** Balance may go negative to a floor of 200 credits; warn from 100 credits; refuse only at the floor, with an actionable message, and auto-resume on top-up. | A venue must never have its books silently stop mid-run. Mirrors the licensing rule that a lapsed licence never disrupts a live trading session. [repo] |
| The credit ledger is **append-only and hash-chained**; the balance is derived from it, never a mutable counter. | A counter drifts and cannot be audited. Same rule as the licensing audit trail. [repo] |
| Credit expiry: every grant is a lot with `expires_at` = grant + 24 months; consumption draws lots down oldest-expiry first. | Credits that never expire sit as deferred revenue forever. Decided 2026-10-18. [pack] |

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

## The client meter that actually exists (corrected 2026-10-18)

An earlier version of this section described a hash-chained `samesync/billing/`
module with a `POST /api/usage` contract as "built, 29 tests, deployed". **That
module does not exist in the samesync repository** (no file, branch, commit or
stash); it was lost with the container that built it. What the shipped client
really has, verified in the repo on 2026-10-18:

- `samesync/metering.py`: a plain-JSON per-day ledger (`usage_ledger.json`)
  keyed `venue|YYYY-MM-DD`, storing the per-day **maximum** so a re-run never
  inflates a day, with an `acked` high-water mark. Unbalanced days are never
  billed.
- Transport is the existing heartbeat: `POST {control_url}/api/checkin` carries
  `usage: [{day, venue, receipts}]` and the server answers `usage_ack`
  (`{day_key: receipts}`). There is no `/api/usage` endpoint anywhere.
- The deployed unit is **POS receipts** on balanced days, billed by the Python
  control plane at `cp.sam-squared.com` at a per-install cents price against a
  mutable money balance. That is the model being replaced.

The billing platform (samesync repo, `billing/`, see its `ARCHITECTURE.md`)
keeps that wire contract byte-compatible and extends it: entries gain `lines`
(QuickBooks import lines actually emitted), the install authenticates with a
per-install token issued at pairing, the server bills the **growth of lines per
customer and venue-day** as credits (on every day the build writes, reconciled
or not; a second install or a renamed venue never bills a day twice) and
acknowledges with `usage_ack` plus `usage_ack_v2`. An acknowledgement means
durably stored; anything that cannot be billed lands in a quarantine table and
is never acknowledged, and a malformed entry is echoed as `usage_rejected` so
the client stops re-sending it. A day the legacy control plane acknowledged was
paid there per receipt and is never billed again on the platform. [repo]

## Decisions taken 2026-10-18

Confirmed by Mr LSG in chat on 2026-10-18. Each is also a row in DECISIONS.md.

| Decision | Value |
|---|---|
| Billing stack | **Stripe + Prisma + Supabase on Next.js App Router** - the pack's documented architecture. Resolves DECISIONS.md tension 3; the Cothenticity-style Cloudflare Pages/D1 + Paystack mirror is dropped for SAMeSync. |
| Credit unit | 1 credit = **one QuickBooks import line**. The client pipeline reports `qbo_line`; `pos_row` stays recorded at zero cost for analytics. |
| AI upload | **R2.50 per upload (250 cents) = 5 credits** at 50c/credit, per document regardless of length. |
| Once-off software fee | **R5 000 incl. VAT**, includes the 500-credit bundle. Stored split: net 434 783c + VAT 65 217c = 500 000c. |
| Top-ups | 50c per credit, VAT inclusive, split stored on every row. |
| Credit expiry | **24 months** from grant; warn from 60 days before expiry. |
| Payment provider | ~~Stripe. Webhook handler idempotent on the Stripe event id (pack rule).~~ **Reopened 2026-10-20:** Stripe does not onboard a South African company, so the platform takes payment through Paystack or Stripe (`PAYMENT_PROVIDER`), each webhook idempotent - a Paystack transaction is keyed on its transaction id whichever way it arrives. **Decided 2026-10-20: Paystack** (`PAYMENT_PROVIDER=paystack`), on Vercel, with the database in eu-west-1 (DECISIONS.md rows of that date). |

## Still open

1. **Voice uploads** - "images and voices into QuickBooks". Not confirmed as a
   planned feature; if it is, it needs its own credit cost (audio transcription
   prices differently from page scans).
2. **Per-document vs per-page AI scan pricing.** Per document is chosen; a
   40-page consolidated supplier statement costs the same as a one-page delivery
   note while costing more to scan. Page counts are recorded either way, so the
   mix can be watched before switching.
3. **SAMeSync's own Paystack business, or Cothenticity's.** Paystack is chosen
   (2026-10-20); a business has one webhook URL per mode, so sharing
   Cothenticity's means settling without the webhook. DECISIONS.md tension 5.
4. **The Supabase plan for the billing project** - the `sam2 pty ltd`
   organisation is on Free with both active slots used; go-live step 1 waits on
   Pro (or a second organisation).
