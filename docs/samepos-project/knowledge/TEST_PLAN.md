# SAMePOS Test Plan

STATUS: DRAFTED 2026-08-25. Part A is verified against the Sam repo and runs today. Part B is a plan, not a suite: most rows have no automation yet. Mr LSG must (1) trim Part B to the rows he will actually run, (2) confirm every [draft] row, and (3) replace [draft] table and endpoint names with real ones once SCHEMA.sql and SYNC_ENGINE.py are uploaded.

**A test plan you will not run is worse than none. Trim hard.**

## How to read this file

Every substantive claim carries a provenance tag:

| Tag | Meaning |
|-----|---------|
| [repo] | Verified against a file in the Sam repo (Sam Squared Softwares). |
| [pack] | Stated by Mr LSG in the SAMePOS setup pack. Authoritative user context. |
| [draft] | Claude's inference or an industry-standard assumption. Confirm before relying on it. |

Every Part B row carries an execution tag:

| Tag | Meaning |
|-----|---------|
| AUTO | Runs as an automated test (pytest, shell script, CI). |
| SQL | Run by hand as SQL against a database, check the result. |
| MANUAL | A human does it on a real or staged terminal. |

Naming note: the licensing schema uses the British spelling "licence" in all object names (`licence`, `licence_config`, `licence_status`). That spelling is part of the schema. Do not "correct" it in SQL. [repo]

---

# PART A: Tests that exist and run TODAY

These live in the Sam repo (the repo that holds the licensing DB core, node-install domain knowledge, and Sema4.ai action tooling). The SAMePOS product source (sync engine, installers, billing app) lives elsewhere and is NOT covered by these. [repo]

## A1. Licensing database harness

```bash
# run from the Sam repo root
bash db/tests/run_db_tests.sh
```

What it does [repo]:

1. Spins up a disposable PostgreSQL cluster in a temp dir. Never touches a real database.
2. Applies the numbered migration `db/migrations/024_licensing.sql` to a DB with `sales_order` and `sales_payment` stubs. Confirms the hard guard attaches at the payment table.
3. Applies the standalone schema `db/schema/licensing.schema.sql` to a bare DB.
4. Runs `db/tests/behavior.sql`: plpgsql ASSERT checks of the full licence lifecycle (details in A2).
5. Asserts the migration and the standalone schema produce the SAME objects.
6. Asserts the migration is idempotent: applies twice cleanly.
7. Asserts the rollback `db/migrations/024_licensing.down.sql` removes every licensing object.

Exit codes [repo]:

| Code | Meaning |
|------|---------|
| 0 | All checks passed. |
| 77 | Skipped: no PostgreSQL server binaries (`initdb`, `pg_ctl`) on this machine. |
| other | A check failed. |

Requires PostgreSQL server binaries. Runs the cluster as an unprivileged user when invoked as root. [repo]

## A2. What behavior.sql proves

The behavioral assertions cover, in order [repo]:

1. Guard trigger attached, and specifically to the PAYMENT table, not tab-open.
2. No node identity yet: state `none`, invalid.
3. Set node plus auto-start trial: valid about 14 days, idempotent on re-call.
4. Expired trial: state `expired`, invalid, 0 days remaining.
5. Grace window: valid plus `in_grace`; grace off: expired again.
6. Signed licence rejected (returns `success=false`) when the app did not verify the signature.
7. Signed licence rejected when bound to a different install code. Both rejects persist in `licence_audit`.
8. Perpetual licensed key accepted; outranks the expired trial.
9. Idempotent by key fingerprint: activating the same key twice yields one row.
10. Extended trial fallback: revoke the licensed key, status falls back to the extended trial.
11. Hard guard behavior: valid licence lets payments through even with enforcement ON; invalid licence plus enforcement ON blocks the payment insert but still allows tab-open; enforcement OFF (advisory) allows payments even when invalid.
12. First-boot concurrency backstop: the partial unique index makes a second auto trial impossible even if a caller bypasses `licence_ensure_trial()`.

## A3. Pytest suite

```bash
# run from the Sam repo root
python3 -m pytest tests/test_licensing_db.py
```

This wraps A1 into pytest (skips cleanly on exit 77). [repo]

The wider suite:

```bash
python3 -m pytest tests/ -q
```

| File | What it proves [repo] |
|------|----------------------|
| `tests/test_licensing_db.py` | The full A1 harness, end to end. |
| `tests/test_sql_guard.py` | The read-only SQL guard for `query_snowflake`: destructive and stacked statements refused, legitimate read-only SQL (comments, semicolons inside string literals) passes. 21 tests. |
| `tests/test_snowflake_analyst.py` | The Snowflake analyst agent: URL construction, HTTP wrapper failure paths, tool schemas, agent loop against a stub. 25 tests. No network needed. |
| `tests/test_managed_agents.py` | Validates the managed subagent definition files. 5 tests. |
| `tests/test_snowflake_readonly_role.py` | The read-only Snowflake role renderer: identifiers cannot smuggle DDL, output contains exactly the intended grants. 15 tests. |

Only the first row is SAMePOS product logic. The rest test the Sam repo's own tooling. [repo]

## A4. Read-only pre-flight for a real node

Not a test, but the safe first step before applying the licensing migration to a live node [repo]:

```bash
psql -d <node_db> -f db/tools/check_reconciliation.sql > reconcile.txt
```

Strictly read-only. Reports name collisions against all 16 objects the migration creates, the migration-tracking table, the payment table the guard should target, and pre-existing licence state. **Any `t` in the collision check means reconcile before applying.** [repo]

---

# PART B: Product test plan (to build)

The product under test [pack]: offline-first event sourcing. Cloud is Supabase (project ref kpjqfsbjksnmxogmmcrb). Local cache is PostgreSQL 16 on port 5433 with CDC triggers. Sync engine is Python, about 900 lines, with conflict resolution. Dashboard is React/TypeScript with dual connection. Windows install: install.bat, PowerShell installer, post-install verifier, Inno Setup .exe with GUI credential wizard. Billing: Stripe plus Prisma plus Supabase on Next.js App Router with an idempotent webhook handler. Currency ZAR, VAT 15 percent inclusive, timezone Africa/Johannesburg, money as integer cents, unique constraint on `(terminal_id, sale_ref)`, POPIA-scoped customer data.

Repo facts that sit next to the pack [repo]: the node runs a FastAPI app with an embedded PostgreSQL, exposes `/api/licence/status` and `/api/licence/activate`, migrates venues from legacy DigitotPOS, and is commissioned with `configure-node.ps1` and `deploy-update.ps1`. The Sam repo also contains a GitHub Actions workflow configured to build and deploy an ASP.NET Core app named "SAMePOS" to Azure App Service on push to main; the repo itself holds no .NET source, so the workflow cannot currently succeed.

Open tensions Mr LSG must reconcile:

| # | Pack says | Repo shows | Question |
|---|-----------|-----------|----------|
| 1 | Cloud layer is Supabase [pack] | A workflow is configured to deploy an ASP.NET Core app "SAMePOS" to Azure App Service, but this repo has no .NET source to build [repo] | What is the Azure app? A second cloud piece deployed from another repo, a legacy piece, or dead? Which rows here test it? |
| 2 | Sync engine is Python [pack] | Node app is FastAPI (Python) [repo] | Is the sync engine inside the FastAPI app or a separate process? Changes how B3 kill tests are run. |
| 3 | Install path is install.bat plus Inno Setup wizard [pack] | Commissioning uses configure-node.ps1 / deploy-update.ps1 and a DigitotPOS import [repo] | Are these the same flow or two flows (fresh install vs migration)? B5 assumes two. |

Rules for every row below: money asserts are in integer cents, VAT split asserts 15 percent inclusive, all timestamps interpreted in Africa/Johannesburg. [pack]

## B1. Sync idempotency

Risk: a duplicate or lost sale. The `(terminal_id, sale_ref)` unique constraint is load bearing. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 1.1 | Ring 50 sales offline. Kill the sync engine process mid-batch (kill -9 or end task). Restart it. [draft] | All 50 sales reach Supabase exactly once. Local and cloud counts match. No partial rows. | MANUAL then SQL |
| 1.2 | Replay: run the same sync batch twice on purpose. [draft] | Second run is a no-op. Row counts unchanged. Constraint absorbs the duplicates, engine logs them as skipped, not errors. | AUTO |
| 1.3 | Duplicate push: insert the same `(terminal_id, sale_ref)` into the cloud from two connections at once. [draft] | One insert wins, one gets a unique-violation the engine handles cleanly. No crash, no retry loop. | SQL |
| 1.4 | Clock skew: set the terminal clock 2 hours wrong, ring a sale, sync, fix the clock, sync again. [draft] | Sale syncs once. Ordering and business-day assignment follow the event data, not the wall clock. Document actual behavior if not. | MANUAL |
| 1.5 | Constraint violation handling: force a `(terminal_id, sale_ref)` conflict where the two rows DIFFER in content. [draft] | The engine flags it as a conflict for resolution. It never silently overwrites, never silently drops. | AUTO |
| 1.6 | Count reconciliation query: total sales and total cents per terminal per day, local vs cloud. [draft] | Identical numbers both sides after sync settles. | SQL |

```sql
-- reconcile_counts.sql [draft: confirm table and column names against SCHEMA.sql]
SELECT terminal_id, COUNT(*) AS sales, SUM(total_cents) AS cents
FROM sales
WHERE business_day = CURRENT_DATE
GROUP BY terminal_id
ORDER BY terminal_id;
-- run on local (port 5433) and on Supabase; diff the output
```

## B2. Cash-up correctness

Risk: staff theft goes unseen, or honest staff get blamed. The detailed rules live in CASHUP_AND_STOCK_RULES.md; these rows test the totals. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 2.1 | Mid-shift staff swap: staff A rings 3 sales, swaps to staff B, B rings 2. Cash up. [draft] | Each sale attributed to the staff member who rang it. Shift total equals A's total plus B's total, in cents, exactly. | MANUAL then SQL |
| 2.2 | Offline void: ring a sale offline, void it offline, reconnect, sync. [draft] | The void survives sync. Cash-up excludes it (or shows it as a void line, per the rules file). Never counted as revenue. | MANUAL |
| 2.3 | Split payment: one sale, part cash part card. [draft] | Cash-up cash total includes only the cash part. Card total only the card part. Parts sum to the sale total in cents. | MANUAL then SQL |
| 2.4 | Refund: refund a synced sale. [draft] | Cash-up shows the refund as negative on the correct payment type. VAT on the refund reverses too. | MANUAL then SQL |
| 2.5 | Multi-terminal same shift: two terminals, same venue, same business day, sales on both. [draft] | Venue cash-up equals the sum of both terminals. No sale counted twice, none missed. | SQL |
| 2.6 | VAT split totals: for any day, sum of (net plus VAT) per sale equals gross, VAT equals gross times 15/115 rounded per the rules file. [pack for the rate, draft for rounding] | Zero cents drift across the day. **Confirm the rounding rule (per line or per sale) in CASHUP_AND_STOCK_RULES.md first.** | SQL |
| 2.7 | Property check: generate 1000 random baskets, compute cash-up in the app and in an independent script. [draft] | Totals match to the cent every time. | AUTO |

## B3. Offline-first

Risk: offline is the normal state. If a feature dies without internet, it is broken. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 3.1 | Pull the network cable mid-sale (basket open, before payment). Complete the sale. [draft] | Sale completes locally with no visible error to staff. It syncs on reconnect. | MANUAL |
| 3.2 | 24 hours offline: trade a full day with no internet, then reconnect. [draft] | Full day syncs. Order preserved. Business day assignment correct in Africa/Johannesburg. Dashboard catches up. | MANUAL |
| 3.3 | Partial sync then power cut: reconnect, let sync start, cut power to the terminal mid-sync. Boot it back up. [draft] | Sync resumes from where it stopped. No duplicates (B1 constraint), no lost sales. | MANUAL then SQL |
| 3.4 | Flapping link: connect and disconnect the network every 30 seconds during trading. [draft] | No duplicate syncs, no crash, no queue corruption. | MANUAL |
| 3.5 | Dashboard dual connection: kill the cloud connection while the dashboard is open. [pack for dual connection, draft for behavior] | Dashboard falls back to the local connection and says so. It does not show stale data as live. | MANUAL |
| 3.6 | CDC trigger check: insert a row locally, confirm the CDC trigger queued it for sync. [pack for CDC triggers, draft for the queue shape] | Exactly one change record per insert. | SQL |

## B4. Licensing

These rows are grounded in the real schema in the Sam repo (`db/schema/licensing.schema.sql`). Most already run automatically in Part A; run them against a staged node too. [repo]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 4.1 | Trial to grace to expired: start a trial, set `expires_at` into the past, set `grace_days` to 3, check status, then set `grace_days` to 0. [repo] | Status walks trial, then `grace` (still `is_valid=true`, `in_grace=true`), then `expired` (`is_valid=false`). | SQL (AUTO in Part A) |
| 4.2 | Activation with wrong install code: call `licence_record_signed` with a `bound_install_code` that does not match the node. [repo] | Returns `success=false` with reason "install code mismatch". No `licence` row created. A `reject` row lands in `licence_audit` and persists. | SQL (AUTO in Part A) |
| 4.3 | Unverified signature: call `licence_record_signed` with `verified=false`. [repo] | Returns `success=false`, reason "signature not verified", audited, no licence row. | SQL (AUTO in Part A) |
| 4.4 | Hard guard OFF (default): expire the licence, leave `hard_enforcement=false`, insert a payment. [repo] | Payment ALLOWED. Enforcement is advisory by default; the app decides what to show. | SQL (AUTO in Part A) |
| 4.5 | Hard guard ON: `UPDATE licence_config SET hard_enforcement = true;` with an invalid licence, insert a payment, then open a tab. [repo] | Payment insert BLOCKED with a clear error. Tab-open still works, so an open shift is never stranded mid-service. | SQL (AUTO in Part A) |
| 4.6 | Same key twice: activate the same signed key twice. [repo] | One `licence` row (unique by key fingerprint). Second call refreshes it, does not duplicate. | SQL (AUTO in Part A) |
| 4.7 | On a staged node: `GET /api/licence/status` and `POST /api/licence/activate` through the FastAPI app, not raw SQL. [repo for the endpoints] | Status matches the `licence_status` view. A rejected activation returns HTTP 400 with the reason. | MANUAL |

```sql
-- licence_expiry_walkthrough.sql [repo: objects verified in db/schema/licensing.schema.sql]
SELECT licence_set_node('<INSTALL_CODE>', '<APP_VERSION>', '<HOSTNAME>');
SELECT * FROM licence_ensure_trial();          -- starts the 14 day trial
SELECT state, is_valid, days_remaining FROM licence_status;   -- expect: trial, t
UPDATE licence SET expires_at = now() - interval '1 day' WHERE mode = 'trial';
UPDATE licence_config SET grace_days = 3;
SELECT state, is_valid, in_grace FROM licence_status;         -- expect: grace, t, t
UPDATE licence_config SET grace_days = 0;
SELECT state, is_valid FROM licence_status;                   -- expect: expired, f
```

Run only on a scratch or staged database. Never on a trading node. [repo: the samepos-specialist rule "never corrupt a trading venue's data"]

## B5. Installer

Risk: one technician, strange Windows machines, no second visit. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 5.1 | Fresh Windows box to first real sale: clean Windows VM, run the Inno Setup .exe, complete the GUI credential wizard, ring a real test sale end to end. [pack for the installer, repo for the "real sale end to end" bar] | Install completes, licensing is active, and a real sale rings up and syncs. **An install is not done until a real sale completes.** | MANUAL |
| 5.2 | Re-run idempotency: run install.bat / the PowerShell installer a second time on the same box. [pack for the scripts, draft for behavior] | No damage, no duplicate services, no reset data. Script says what it skipped. | MANUAL |
| 5.3 | Post-install verifier: run it on a good install, then on a deliberately broken one (stop the local PG service first). [pack for the verifier, draft for behavior] | Passes the good box. Names the exact failing check on the broken box. | MANUAL |
| 5.4 | Wizard bad input: enter wrong Supabase credentials in the GUI wizard. [draft] | Clear failure message at the wizard, not a half-installed box. Placeholders like <SUPABASE_ANON_KEY> never end up literally in config. | MANUAL |
| 5.5 | Migration flow: on a box with DigitotPOS data, run the commissioning sequence (configure-node.ps1, import, deploy-update.ps1). [repo] | Digitot source data untouched (read-only import). Catalog, staff, and menu counts reconcile between source and node. | MANUAL then SQL |
| 5.6 | Port check: confirm the local PostgreSQL listens on 5433 and the app points at it. [pack] | `netstat` or `Get-NetTCPConnection` shows 5433. A sale writes to it. | MANUAL |

## B6. Billing webhooks

Risk: double charge or double provision. The handler is Next.js App Router with Stripe, Prisma, Supabase, and a WebhookEvent model. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 6.1 | Replay same event id: send the same Stripe event twice (Stripe CLI `stripe trigger`, then resend). [pack for idempotency, draft for mechanism] | Second delivery is a no-op. One WebhookEvent row. One provision. One charge record. | AUTO |
| 6.2 | Out-of-order events: deliver `invoice.paid` before the `checkout.session.completed` it depends on. [draft] | Handler tolerates the order: defers, retries, or reconciles. Final state is correct either order. | AUTO |
| 6.3 | Partial failure mid-handler: make the DB write fail after the event is marked received (kill the DB connection in a test). [draft] | Handler returns non-2xx so Stripe retries. The retry then succeeds exactly once. No state where the event is marked done but the work is not. | AUTO |
| 6.4 | Unknown event type: send an event type the handler does not handle. [draft] | 2xx and logged. Not an error loop. | AUTO |
| 6.5 | Signature check: send a payload with a bad Stripe signature. [draft] | Rejected before any DB write. | AUTO |

## B7. POPIA and data

Risk: personal data leaks through logs or over-collection. Customer fields are POPIA scoped: collect the minimum, never log personal data in plain text. [pack]

| # | What to do | Expected result | Tag |
|---|-----------|-----------------|-----|
| 7.1 | Grep all logs (terminal, sync engine, installer, webhook) after a full test day for names, phone numbers, email addresses, and ID numbers used in test data. [draft] | Zero hits. Identifiers appear only as opaque ids. | AUTO |
| 7.2 | Column audit: list every column in customer-related tables against the rules file's minimum-field list. [draft, needs SCHEMA.sql] | No column outside the approved minimum. Anything extra is a finding. | SQL |
| 7.3 | Error path leak: force an exception during a sale with a customer attached, read the stack trace and any error report. [draft] | No personal data in the trace or report. | MANUAL |
| 7.4 | Sync payload check: capture one sync batch and inspect it. [draft] | Personal fields present only where the cloud schema needs them, transported over TLS, never written to a temp file in plain text. | MANUAL |

---

## Suggested run cadence [draft]

| When | Run |
|------|-----|
| Every commit to the licensing core | Part A (all of it, it is fast). |
| Before any release | B1.2, B1.5, B2.7, B6.1 to B6.5 (the AUTO rows). |
| Before commissioning a new venue | B5.1, B5.3, B5.5, B4.7, then one full B3.2 day on the staged node. |
| Quarterly | B7 in full. |

## Open gaps for Mr LSG

1. Confirm or correct every [draft] row. The table and column names in the SQL snippets are guesses until SCHEMA.sql is in this project.
2. Reconcile the three tensions in the Part B intro (Azure app, sync engine shape, one install flow or two).
3. State the VAT rounding rule (per line or per sale) in CASHUP_AND_STOCK_RULES.md so B2.6 can be pinned.
4. Name the product's real payment table so the licence hard guard attaches to the right one (the migration probes `sales_payment`, `sale_payment`, `order_payment`, `payment`). [repo]
5. Decide which Part B rows get automated first. Suggestion: B1.2 and B2.7, they catch the most expensive bugs.
