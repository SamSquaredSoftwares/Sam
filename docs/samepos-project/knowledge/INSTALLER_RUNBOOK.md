# INSTALLER_RUNBOOK.md - SAMePOS Node Install and Commissioning

STATUS: SKELETON. Built from verified repo knowledge (the Sam repo's samepos-specialist agent file and db/README.md). The ten-phase PowerShell detail is NOT here yet. To finish this file: (1) paste the ten phases from the product's installer README into the "TEN PHASES" section below, (2) add a field note after every site visit, (3) run starter prompt 3 ("Installer field test") to regenerate the pre-drive checklist and the failure triage properly.

Provenance labels used in this file:

| Label | Meaning |
|-------|---------|
| [repo] | Verified against a file in the Sam repo (Sam Squared Softwares) |
| [pack] | Stated by Mr LSG in the project setup pack |
| [draft] | Inference or standard assumption. Mr LSG must confirm |

---

## 1. What a SAMePOS node is

- A SAMePOS node is one venue box: a Windows PC at a bar, club, restaurant, or liquor store. [repo]
- It runs an on-site POS server and replaces or migrates from the legacy DigitotPOS system. [repo]
- The node runs a FastAPI app against an embedded PostgreSQL database. [repo]
- The node is identified by a machine-bound install code. [repo]
- Pack context: the local database is PostgreSQL 16 on port 5433 with CDC triggers, syncing to Supabase via a Python sync engine. [pack]
- Tension to reconcile: the repo describes the node app as FastAPI with embedded PostgreSQL. The pack describes a Python sync engine plus a React/TypeScript dashboard, and the repo also holds a GitHub Actions workflow deploying an ASP.NET Core app named "SAMePOS" to Azure App Service. [repo] These are probably different components (node app, sync engine, cloud app). **Mr LSG: confirm which component the installer installs, and how the pieces map.** [draft]

---

## 2. The overriding rule

**Never corrupt a trading venue's data.** [repo]

A node holds a live venue's catalog, staff, menu, pricing, and sales. Mistakes cost real money and downtime. [repo]

Safety rules, all [repo]:

1. Never run destructive operations against a live or production database.
2. Build and validate in a fresh or scratch database first. Cut over deliberately.
3. Back up existing data before any migration or update step.
4. Treat the Digitot import as read-only against the source. Never mutate the source catalog.
5. If a step is ambiguous or could touch live data, stop and confirm before proceeding.
6. Before applying the licensing migration (024) to a real node, run the read-only pre-flight `db/tools/check_reconciliation.sql` from the Sam repo. It reads catalogs and counts rows only. Any collision flag means reconcile first.

```bash
psql -d <node_db> -f db/tools/check_reconciliation.sql > reconcile.txt
```

Pack rule that also applies here: every migration ships with a rollback script. The licensing core ships `024_licensing.down.sql`. [repo] [pack]

---

## 3. The commissioning sequence

Authoritative order, from the samepos-node-install runbook as summarized in the repo's samepos-specialist agent. Do not improvise the order. [repo]

1. Reach the target machine.
2. Transfer the payload.
3. Build a fresh database.
4. Import the live Digitot catalog, staff, and menu. The source is read-only. Never mutate it.
5. Apply the update build.
6. License the node.
7. Verify a real sale rings up end-to-end.

**An install is not done until a real sale completes end-to-end and licensing is active.** [repo]

Verification checklist at the end of every install [repo]:

- Database built.
- Digitot data imported and reconciled.
- Update applied.
- License active.
- A real sale rings correctly.

---

## 4. Known scripts and install artifacts

Scripts named in the repo [repo]:

| Script | Role |
|--------|------|
| `configure-node.ps1` | Node configuration (PowerShell) |
| `Configure-Node.bat` | Batch wrapper for the above [draft: wrapper role inferred] |
| `deploy-update.ps1` | Applies an update build to a node |

PowerShell standard for all of these [repo]:

- Idempotent. Safe to re-run.
- Logs what it does.
- Checks preconditions before acting.
- Fails loudly and safely. No half-applied state.

Install artifacts per the pack [pack]:

| Artifact | Role |
|----------|------|
| `install.bat` | Entry point on the target machine |
| PowerShell installer | The main install logic (the ten phases) |
| Post-install verifier | Confirms the install worked |
| Inno Setup `.exe` | Packaged installer with a GUI credential wizard |

Open question: how `install.bat` and the Inno Setup `.exe` relate to `configure-node.ps1` and `deploy-update.ps1` is not documented in the Sam repo. **Mr LSG: map artifacts to scripts when pasting the ten phases.** [draft]

---

## 5. TEN PHASES (paste from the product's installer README)

The ten-phase detail lives in the product repo's installer README, which is not in the Sam repo. Paste each phase here: what it does, what it checks, what its log line looks like, and what "done" means for that phase. [pack: that ten phases exist]

1. Phase 1: TODO
2. Phase 2: TODO
3. Phase 3: TODO
4. Phase 4: TODO
5. Phase 5: TODO
6. Phase 6: TODO
7. Phase 7: TODO
8. Phase 8: TODO
9. Phase 9: TODO
10. Phase 10: TODO

Note: the seven-step commissioning sequence in section 3 is the verified outer shape. The ten phases are the installer's internal breakdown. When pasting, note which commissioning step each phase belongs to. [draft]

---

## 6. Licensing on site

From the Sam repo's licensing core (db/README.md and db/schema/licensing.schema.sql). All [repo] unless marked.

- Standard trial: 14 days by default, auto-started on first boot via `licence_ensure_trial()`. Idempotent: it is a no-op if the node already has any licence.
- Trial length honors the product env var `SAMEPOS_TRIAL_STANDARD_DAYS`, or the `licence_config.standard_trial_days` row.
- Signed licences: Ed25519 keys with the `SPOS1.` prefix, bound to the node's install code. The private signing key lives only in the vendor keystore.
- Activation: `POST /api/licence/activate`. The app verifies the Ed25519 signature, then calls `licence_record_signed(...)`. Rejections return `success=false` with a reason and are audited.
- Status: `GET /api/licence/status` reads the `licence_status` view. It returns mode, state (`trial`, `extended_trial`, `licensed`, `grace`, `expired`, `none`), `is_valid`, `expires_at`, and `days_remaining`.
- First boot sequence:

```text
SELECT licence_set_node(:install_code, :app_version, :hostname);
SELECT licence_ensure_trial();
```

- Enforcement is advisory by default. An optional hard guard blocks payment inserts when the licence is invalid, but only after an admin sets `licence_config.hard_enforcement = true`. It ships false.
- The guard targets the payment insert, not tab-open, so a shift in progress can keep serving. It cannot take money on a dead licence.
- Migration caution: the licensing core (migration 024) was authored from the documented model, not diffed against the product's own migrations 001-023. It has never been run on a real node. Run the pre-flight first (section 2, rule 6).
- If the node already stores a trial start or an activated key elsewhere, migrate that state in. Do not let `licence_ensure_trial()` start a fresh trial and silently extend the term.

---

## 7. Known gotchas (numbered; grow this list on every site visit)

1. Gotcha #4 (the runbook's own numbering): NOT NULL violation on `sales_order.business_day_id` at `POST /api/orders`. Rows are created when staff open a tab. If no business day exists, the insert fails. [repo]
2. Duplicate business_day race: concurrent callers can each pass a check-then-insert and create duplicate rows. The product hit this on `business_day`. [repo] The licensing core guards its own trial insert against the same race (several tablets hitting the node at first boot) with an advisory lock plus a partial unique index. [repo]
3. TODO: paste gotchas #1 through #3 and #5+ from the product's installer README. [draft: their existence is implied by "#4" being the runbook's numbering]
4. TODO: slot for the next field note.
5. TODO: slot.

Field note format [draft]: date, venue, phase that failed, symptom, root cause, fix, and whether the fix is now in the installer.

---

## 8. Pre-drive checklist [draft]

Everything below is a draft. Starter prompt 3 in the project setup pack regenerates this properly from the full runbook. Confirm each line before trusting it.

1. Payload on a USB stick AND a cloud link. Two transfer paths.
2. The latest update build, version noted.
3. Licence key for this node generated, or a plan to run on trial.
4. The node's install code procedure known (how it is generated on the machine).
5. Digitot source location on the target machine known, and confirmed read-only access.
6. Backup target ready: enough free disk or an external drive for the pre-migration backup.
7. `check_reconciliation.sql` on the stick if the licensing migration will be applied.
8. Credentials ready as placeholders to type on site, never stored in the payload: `<SUPABASE_URL>`, `<SUPABASE_SERVICE_KEY>`, `<NODE_DB_PASSWORD>`.
9. Port check plan: confirm nothing else owns port 5433 on the target. [pack: 5433 is the local PG port]
10. A test sale plan agreed with the venue: one real low-value item, rung and voided or paid per venue policy.
11. Phone tethering ready. Venue internet is assumed bad. [pack]
12. Remote access fallback (e.g. AnyDesk/RustDesk ID) in case a second visit must be avoided. [draft]

---

## 9. Phase-failure triage [draft]

Draft decision tree. Starter prompt 3 will rebuild this once the ten phases are pasted in.

| Situation | First move | Then |
|-----------|-----------|------|
| A phase fails before the database is built | Fix and re-run the installer. Scripts are idempotent, so re-running is safe. [repo: idempotency standard] | Log the failure as a field note |
| A phase fails after the database is built but before cutover | Stay in the scratch database. Never patch the live one. [repo] | Re-run from the failed phase or rebuild the scratch DB |
| Digitot import fails or reconciles wrong | Stop. The source is read-only, so nothing is lost. [repo] | Re-check source paths and encodings, re-import into a fresh scratch DB |
| Update build fails to apply | Restore from the pre-update backup. [repo: back up before any update] | Retry with the previous known-good build |
| Licensing fails to activate | Node keeps working on trial. Enforcement is advisory by default. [repo] | Check `licence_status`, the install code binding, and the audit trail in `licence_audit` |
| Test sale fails at POST /api/orders | Suspect gotcha #4: missing business day. [repo] | Ensure a business day exists, retry the sale |
| Anything at all touching live trading data looks ambiguous | Stop and confirm with a human. [repo] | Do not proceed on a guess |

**If in doubt, the safe state is: scratch DB intact, live DB untouched, backup taken.** [repo]

---

## 10. Open items for Mr LSG

1. Paste the ten phases (section 5).
2. Paste gotchas #1-#3 and any after #4 (section 7).
3. Confirm the component map: FastAPI node app vs Python sync engine vs ASP.NET Core Azure app (section 1).
4. Map install artifacts to scripts (section 4).
5. Confirm the product's real payment table name so the licence hard guard attaches to the right table. The migration probes `sales_payment`, `sale_payment`, `order_payment`, `payment` in that order. [repo]
6. Run starter prompt 3 to replace sections 8 and 9 with the real checklist and decision tree.
