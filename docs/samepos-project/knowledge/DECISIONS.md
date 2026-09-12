# DECISIONS.md - SAMePOS Decision Log

STATUS: Seeded draft, written 2026-08-25. Every row below is a decision taken from the setup pack (Mr LSG's own stack facts) or from the Sam repo (verified files). To finish this file: 1) confirm every "why" tagged [draft], 2) answer the two open tensions at the bottom, 3) supply the missing "why" for the Azure App Service deploy.

## What this file is

SAMePOS is an offline-first point of sale for South African retail and hospitality, built by Sam Squared Softwares (Pty) Ltd. This log records why the big calls were made, one line each, so they never get re-litigated in chat.

Provenance tags:

| Tag | Meaning |
|-----|---------|
| [pack] | Stated by Mr LSG in the project setup pack. Authoritative. |
| [repo] | Verified in a file in the Sam repo (licensing DB core, node-install agent, deploy workflow). Note: the Sam repo is NOT the product source. The sync engine, installers, and billing app live elsewhere. |
| [draft] | Inferred or industry-standard. Mr LSG must confirm or correct. |

A row's Provenance tag covers the Decision. Where the Why is a guess, the [draft] tag sits inside the Why cell.

Dates: "earlier" means the decision predates this log. Do not backfill guessed dates.

## The log

| Date | Decision | Why | Provenance |
|------|----------|-----|------------|
| earlier | Architecture is offline-first event sourcing. Terminals write locally and sync immediately on reconnect. | Venues run cheap Windows terminals with bad internet. Offline is the normal state, not the error state. | [pack] |
| earlier | Supabase is the cloud layer: Postgres, Auth, RLS, Realtime, Edge Functions. | One managed platform covers database, auth, row security, realtime, and serverless. [draft] | [pack] |
| earlier | Local cache and fallback is PostgreSQL 16 on port 5433, with CDC triggers feeding sync. | Port reason not stated. Likely to avoid clashing with any default PostgreSQL on 5432 on the same machine. [draft] | [pack] |
| earlier | Money is stored as integer cents. Never float. Display ZAR with 2 decimals. | Floats drift on arithmetic. Integer cents keep totals exact and audits clean. [draft] | [pack] |
| earlier | VAT is 15 percent and inclusive by default. Every money calculation shows the VAT split. | 15 percent is the South African VAT rate, and retail prices are quoted VAT inclusive. [draft] | [pack] |
| earlier | Trusted backend writers use the Supabase service-role key. Clients and terminals use RLS scoped by venue. Never mix the two. | Backend jobs must write across venues, so they bypass RLS. Client-side keys must never be able to. [draft] | [pack] |
| earlier | The unique constraint on (terminal_id, sale_ref) is the idempotency anchor for sync. | Sync must stay idempotent. Re-sync after a dropped connection must not duplicate or lose a sale. The constraint is load bearing. | [pack] |
| earlier | Billing is Stripe plus Prisma plus Supabase, on Next.js App Router, with an idempotent webhook handler. | Stripe carries cards and subscriptions. The idempotent handler stops replayed webhooks from double charging or double provisioning. [draft] | [pack] |
| earlier | Customer fields are POPIA scoped. Collect the minimum. Never log personal data in plain text. | POPIA is South Africa's data protection law. Less data held is less liability. [draft second sentence] | [pack] |
| earlier | SAMePOS replaces and migrates from legacy DigitotPOS. The Digitot source is treated as strictly read-only during import. | Migration reason not stated. [draft]: target venues already run Digitot, so migration is the sales path. Read-only source: never corrupt a trading venue's data. | [repo] |
| earlier | An install is not done until licensing is active and a real sale rings up end-to-end. | Verification is not optional. A node that cannot ring a sale is not commissioned. | [repo] |
| earlier | Node work happens in a fresh or scratch database first, then cuts over deliberately. Back up before any migration or update. | Mistakes on a live node cost the venue real money and downtime. | [repo] |
| earlier | Licenses are Ed25519-signed "SPOS1." keys. The signature is verified in the app, not the database. The DB is system of record and enforcement point only. | Stock PostgreSQL cannot verify Ed25519 signatures. The app (FastAPI on the node) verifies with the vendor public key and passes the result in. The private signing key lives only in the vendor keystore. | [repo] |
| earlier | Each license key is bound to the node's machine-bound install code. | A key only works on the machine it was issued for. The DB rejects a mismatched install code and writes an audited reject. | [repo] |
| earlier | Standard trial is 14 days, auto-started on first boot. Tunable via `licence_config.standard_trial_days` or the `SAMEPOS_TRIAL_STANDARD_DAYS` env var. | Why 14 days is not stated. [draft]: long enough to trade through two weekends. | [repo] |
| earlier | License enforcement is advisory by default. A hard guard exists but is gated by `licence_config.hard_enforcement`, which ships false. | The app owns user-facing behavior (banners, refusing new tabs). Hard blocking on a trading venue is an admin opt-in, never a silent default. | [repo] |
| earlier | The hard guard blocks payment inserts, not tab opens. | Guarding tab opens could strand an open shift mid-service. Guarding payments lets the shift keep serving while the node cannot take money on a dead license. | [repo] |
| earlier | The first-boot trial race is closed with `pg_advisory_xact_lock(hashtext(install_code))` plus a partial unique index (`licence_one_auto_trial_uidx`) as the database backstop. | Several tablets hitting a node at first boot can each pass a check-then-insert and create duplicate trials. Same race the product hit on `business_day`. | [repo] |
| earlier | Rejected license activations return success=false with a reason instead of raising an exception. | PostgreSQL has no autonomous transactions. A RAISE would roll back the reject audit row. Returning a status lets the audit trail commit and maps cleanly to HTTP 400. | [repo] |
| earlier | The licensing core ships as migration 024: additive, idempotent, wrapped in a transaction, with a `024_licensing.down.sql` rollback. | It touches a venue's financial database. Additive plus rollback honors the rule: no destructive change without a way back. | [repo] |
| earlier | A strictly read-only reconciliation pre-flight (`check_reconciliation.sql`) must run on a live node before migration 024 is applied. | The core was authored from documented behavior, not diffed against the product's own migrations 001-023. `CREATE ... IF NOT EXISTS` would silently adopt a colliding object of the wrong shape. The pre-flight is safe on a trading node because it only reads. | [repo] |
| earlier | A GitHub Actions workflow is configured to deploy an ASP.NET Core build to the Azure Web App "SAMePOS" (Production slot) on push to main. The Sam repo holds no .NET source, so the workflow as committed cannot succeed. | **Why is not recorded anywhere. Ask Mr LSG.** See tension 1 below. | [repo] fact, why missing |

## Open tensions to reconcile

Both sides of each tension are real. Neither is dropped. Mr LSG decides.

1. Cloud layer. The pack says Supabase is the cloud layer [pack]. The repo's deploy workflow ships an ASP.NET Core app called SAMePOS to Azure App Service [repo]. Is the Azure app legacy, a separate admin or vendor app, or part of the current stack? Record the answer as a new row.
2. The node app. The repo's licensing core assumes a FastAPI app on the node calling `POST /api/licence/activate`, backed by an embedded PostgreSQL [repo]. The pack describes a Python sync engine of roughly 900 lines [pack]. Same service, or two services on the node? Also confirm the embedded PostgreSQL is the port-5433 instance the pack names.

3. Billing stack and payment provider for SAMeSync self-serve purchase. The pack says billing is Stripe plus Prisma plus Supabase on Next.js App Router [pack]. Mr LSG separately asked for the SAMeSync storefront to be built "the same way we set up the Cothenticity platform end to end" and chose Paystack when asked - and Cothenticity runs on Cloudflare Pages Functions plus D1 with a reference-keyed `payments` table, a different stack and a different provider. Cothenticity is a different product, so mirroring it is a real choice rather than an error, but only one of the two can be the SAMeSync answer. See MONETIZATION.md open decision 5.

## How to add a decision

1. One line per decision. Add it the day you make it, with the real date.
2. Never delete a superseded row. Strike it (~~like this~~) and add the new row.
3. If the why is a guess, tag it [draft] until confirmed.
