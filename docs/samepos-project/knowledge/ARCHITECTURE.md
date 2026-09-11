# ARCHITECTURE.md - SAMePOS Event Flow

STATUS: DRAFT FOR REVIEW. Claude drafted this from the setup pack plus the Sam repo.
Every claim carries a provenance tag. Mr LSG must: (1) confirm or correct every
[draft] line, (2) answer the "Known unknowns" list, (3) resolve the .NET/Azure
question in section 6. Then delete this paragraph and change STATUS to CURRENT.

Provenance tags used in this file:

| Tag | Meaning |
|-----|---------|
| [pack] | Stated by Mr LSG in the project setup pack. Authoritative intent. |
| [repo] | Verified in the Sam repo (licensing DB core, agent docs, CI workflow). |
| [draft] | Claude's inference. Plausible but unconfirmed. Correct or confirm it. |

Note: this doc lives alone in a Claude Project. It does not assume access to any
repo. The Sam repo holds the licensing DB core and node-install knowledge, not
the product source. The product (sync engine, installers, billing app) lives
elsewhere. [repo]

---

## 1. The system in one paragraph

SAMePOS is an offline-first point of sale for South African retail and
hospitality, built by Sam Squared Softwares (Pty) Ltd. First live venue: Booth
Liquor. [pack] Staff ring up sales on cheap Windows terminals with bad internet.
[pack] Everything lands in a local PostgreSQL 16 first. A Python sync engine
pushes events to Supabase when a connection exists. A React dashboard reads from
both. [pack] **Offline is the normal state, not the error state.** [pack]

Currency is ZAR. VAT is 15 percent, inclusive by default. Timezone is
Africa/Johannesburg. Money is stored as integer cents, never floats. [pack]

---

## 2. Event flow diagram

```text
VENUE SIDE (works with zero internet)
=====================================

 +---------------------+   +---------------------+
 | POS terminal 1      |   | POS terminal N      |    Windows, cheap hardware,
 | (Windows)           |   | (Windows)           |    bad internet [pack]
 +----------+----------+   +----------+----------+
            |                         |
            |  sales, tabs, payments over the venue LAN
            |  via the node's FastAPI HTTP API [draft: repo shows
            |  POST /api/orders, /api/licence/*; terminal transport
            v  is inferred]
 +---------------------------------------------------+
 | ON-SITE NODE: one Windows PC per venue [repo]     |
 |                                                   |
 |  FastAPI app [repo]                               |
 |     |                                             |
 |     v                                             |
 |  PostgreSQL 16, port 5433, embedded [pack+repo]   |
 |   - system of record for the venue                |
 |   - CDC triggers capture changes [pack]           |
 |   - licensing core: licence tables, trial,        |
 |     licence_status view, payment guard [repo]     |
 +------------------------+--------------------------+
                          |
                          |  change feed from CDC triggers
                          |  [draft: exact handoff mechanism unconfirmed]
                          v
 +---------------------------------------------------+
 | PYTHON SYNC ENGINE (~900 lines) [pack]            |
 |  - conflict resolution [pack]                     |
 |  - idempotent: unique (terminal_id, sale_ref)     |
 |    makes re-sync safe [pack]                      |
 |  - queues while offline, pushes on reconnect      |
 |    [pack: "immediate sync on reconnect"]          |
 +------------------------+--------------------------+
                          |
   ~~~~~~~~ internet ~~~~~|~~~~ (often down; that is fine) ~~~~~~~~
                          v
CLOUD SIDE
==========

 +---------------------------------------------------+
 | SUPABASE (project ref kpjqfsbjksnmxogmmcrb)       |
 |  Postgres | Auth | RLS | Realtime | Edge Fns      |
 |  [pack]                                           |
 +-----+---------------------------+-----------------+
       |                           |
       | Realtime + RLS reads      | Edge Function (Deno/TS):
       | scoped by venue [pack]    | AI supplier invoice scanning,
       |                           | calls Claude [pack]
       v                           | [draft: invoked from dashboard
 +---------------------+           |  upload; trigger path unconfirmed]
 | REACT/TS DASHBOARD  |
 | dual connection:    |
 |  cloud (Supabase)   |
 |  + local (node)     |  <--- local path back to the venue node [pack]
 | [pack]              |       [draft: direct PG vs FastAPI unconfirmed]
 +---------------------+

 SIDE SYSTEMS (cloud)
 +----------------------------+     +---------------------------+
 | BILLING APP                |<--->| Stripe                    |
 | Next.js App Router,        |     | webhooks handled          |
 | Stripe + Prisma + Supabase |     | idempotently              |
 | [pack]                     |     | (WebhookEvent model)      |
 +----------------------------+     +---------------------------+

 +-------------------------------------------------------------+
 | ASP.NET CORE APP named "SAMePOS" -> Azure App Service [repo] |
 | Workflow verified; a working deploy is not. Role: OPEN.      |
 | See section 6.                                               |
 +-------------------------------------------------------------+
```

---

## 3. Offline-first posture

**Rule: every feature must work with zero internet and reconcile later.** [pack]

| Concern | Offline (normal state) | On reconnect |
|---------|------------------------|--------------|
| Sales, tabs, payments | Written to local PG 16 on port 5433. [pack] | Sync engine pushes to Supabase. [pack] |
| Change capture | CDC triggers record changes locally. [pack] | Sync engine drains the change feed. [draft] |
| Duplicates | Impossible to double-post a sale: unique (terminal_id, sale_ref). [pack] | Re-sync of the same event is a safe no-op. [pack] |
| Conflicts | N/A while offline. | Sync engine resolves conflicts. Rules live in SYNC_ENGINE.py (knowledge file #4). [pack] |
| Licensing | Fully local. Trial state, license validity, and the payment guard all live in the node's PG. No cloud call needed. [repo] | Nothing to reconcile. [repo] |
| Dashboard | Local connection to the venue node keeps it usable. [pack] | Cloud connection adds Realtime and cross-venue views. [draft] |
| Invoice scanning | Needs internet: Edge Function calls Claude. [pack] Degrades to manual entry offline. [draft] | Scan when connectivity returns. [draft] |
| Billing | Cloud-only (Stripe). Venue trading does not depend on it. [draft] | Webhooks replay safely: idempotent handler. [pack] |

The sync engine must stay idempotent. **The unique constraint on
(terminal_id, sale_ref) is load bearing.** Never change anything that breaks
re-sync safety. [pack]

---

## 4. The on-site node (verified in the Sam repo)

One venue = one Windows PC = one node. [repo]

- The node runs a FastAPI app with an embedded PostgreSQL (developed and tested
  against PG 16). [repo]
- It replaces and migrates from the legacy DigitotPOS system. The Digitot import
  is read-only against the source: catalog, staff, and menu come across; the
  source is never mutated. [repo]
- Commissioning is scripted PowerShell: configure-node.ps1, Configure-Node.bat,
  deploy-update.ps1. Scripts are idempotent, log what they do, and fail loudly.
  [repo]
- An install is not done until a real sale rings up end-to-end and licensing is
  active. [repo]

Licensing core, from db/README.md and db/schema/licensing.schema.sql: [repo]

- Node identity is a machine-bound install code (singleton `licence_node` row).
- A 14-day standard trial auto-starts on first boot (`licence_ensure_trial()`),
  race-safe via an advisory lock plus a partial unique index.
- Paid licenses are Ed25519-signed `SPOS1.` keys bound to the install code,
  activated via `POST /api/licence/activate`. The FastAPI app verifies the
  signature; the database records the verified result and is the system of
  record.
- `licence_status` view answers `GET /api/licence/status`: state is one of
  trial, extended_trial, licensed, grace, expired, none.
- Enforcement is advisory by default. An optional hard guard (a BEFORE INSERT
  trigger on the payments table, shipped OFF) blocks taking money on a dead
  license, but never blocks opening or serving a tab mid-shift.
- Caveat: this core has not yet been reconciled against the product's own
  migrations 001-023, and the product's real payment table name is not pinned
  down. [repo]

Spelling note: prose in this doc uses American "license", but the database
objects are literally named `licence_*`. Use the exact names in SQL. [repo]

---

## 5. Windows install path (product side)

The install path is: install.bat, a PowerShell installer, a post-install
verifier, and an Inno Setup .exe with a GUI credential wizard. [pack]

How this relates to the repo's configure-node.ps1 / deploy-update.ps1
commissioning scripts is not confirmed. Likely the same toolchain described
from two angles, but treat that as unresolved. [draft] Details belong in
INSTALLER_RUNBOOK.md (knowledge file #6).

---

## 6. The cloud .NET app: workflow real, deploy unproven, role unexplained

Two verified facts, one open question.

Fact 1: the Sam repo contains a GitHub Actions workflow
(.github/workflows/main_samepos.yml) CONFIGURED to build an ASP.NET Core app
on windows-latest with dotnet and deploy it to an Azure App Service Web App
named "SAMePOS", Production slot, on every push to main. [repo]

Fact 2: the Sam repo holds no .NET source at all. No .sln, no .csproj, no .cs
file anywhere. So `dotnet build` at the repo root cannot succeed, and the
workflow as committed cannot actually deploy anything. [repo]

The workflow file is real and verified. A working deploy is not.

The setup pack never mentions .NET or Azure. The pack's cloud layer is
Supabase, and its listed apps are Python, React/TS, Deno/TS, and Next.js. [pack]

All facts stand. Do not drop any. Possible readings, all unconfirmed:

| Reading | Implication | [draft] |
|---------|-------------|---------|
| Legacy or parked app, predates the Supabase design | Safe to ignore in new work, should be documented as retired | [draft] |
| A live admin, licensing, or vendor portal deployed from a DIFFERENT repo | It is a real backend writer and needs a place in this diagram and in the security model | [draft] |
| An Azure portal scaffold or experiment that never shipped | Delete the workflow to stop confusion. Fact 2 (no .NET source here) is strong evidence for this reading | [draft] |

QUESTION FOR MR LSG: what does the Azure "SAMePOS" web app serve today, relative
to the Supabase layer? Is it live? Does anything in production call it? Answer
here, retag as [pack], and redraw section 2 if needed.

---

## 7. Security boundaries

| Boundary | Rule |
|----------|------|
| Backend writers | Trusted backend writers (sync engine, Edge Functions, billing) use the Supabase service role key. [pack] |
| Clients and terminals | Use RLS scoped by venue. Never the service role key. Never mix the two. [pack] |
| Customer data | POPIA scoped. Collect the minimum. Never log personal data in plain text. [pack] |
| Secrets | Never write a real secret in any file or chat. Placeholders only, like <SUPABASE_SERVICE_KEY>. If a real key leaks, rotate it. [pack] |
| License crypto | The Ed25519 private signing key lives only in the vendor keystore, never on a node. Nodes hold only public keys. [repo] |
| Node blast radius | A compromised node exposes one venue's local DB, not the fleet: cloud access is scoped by venue RLS. [draft] |

---

## 8. Known unknowns

What the next engineer would ask. Each needs an answer from Mr LSG or the code.

1. Where does the sync engine run: on the node itself, or as a separate
   service? [draft: assumed on the node]
2. What is the CDC handoff: do triggers write an outbox table the engine polls,
   or something else? [draft]
3. Does the dashboard's local connection hit the node's PG directly or go
   through the FastAPI app? [draft]
4. Do terminals run their own app with local storage, or are they thin clients
   of the node? [draft: assumed thin-ish clients of the node]
5. What is the product's real payment table name? The licensing hard guard
   probes sales_payment, sale_payment, order_payment, payment. [repo]
6. Has the licensing core (migration 024) been reconciled against the product's
   migrations 001-023? As of this draft, no. [repo]
7. What does the Azure ASP.NET Core "SAMePOS" app serve? (Section 6.)
8. Does the billing app's Prisma schema point at the same Supabase Postgres or
   a separate database? [draft]
9. What triggers the invoice-scanning Edge Function: a dashboard upload, a
   storage event, or a manual call? [draft]
10. Conflict resolution rules: last-write-wins, field-level merge, or custom
    per table? The answer is in SYNC_ENGINE.py; summarize it here once
    uploaded. [draft]

---

NEXT ACTION: Mr LSG reads this top to bottom, fixes every [draft], answers
section 8, and changes STATUS to CURRENT.
