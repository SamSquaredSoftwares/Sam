# SAMePOS Claude Project: Starter Prompts

Paste these into the project once the knowledge files are in.

Run prompt 1 first. It stress-tests the two files that matter most
(`SYNC_ENGINE.py` and `SCHEMA.sql`) and shows you where the pantry is thin.

## 1. Prove the sync is bulletproof

```
Role: lead engineer on SAMePOS.
Context: I need proof the sync engine is idempotent under real failure.
Inputs: SYNC_ENGINE.py, SCHEMA.sql.
Constraints: offline-first, (terminal_id, sale_ref) unique constraint.
Command: find every path where a duplicate or lost sale is possible.
Rank by likelihood times damage. Give 3 fixes.
Format: table of failure modes, then the code fix for the top one,
then the exact SQL to reproduce the bug before and after.
```

## 2. Cash-up cannot be wrong

```
Role: lead engineer on SAMePOS.
Context: cash-up is where staff steal and where trust dies.
Inputs: CASHUP_AND_STOCK_RULES.md, SCHEMA.sql.
Constraints: ZAR, 15 percent VAT, offline terminals, multi staff shifts.
Command: list every edge case that produces a wrong cash-up total.
Include mid shift staff swaps, offline voids, split payments, and refunds.
Give me the 3 highest risk gaps and the code or schema fix for each.
Format: table then code. End with the SQL test to run tonight.
```

## 3. Installer field test

```
Role: lead engineer on SAMePOS.
Context: I install on strange Windows machines with no support on site.
Inputs: INSTALLER_RUNBOOK.md.
Constraints: one technician, one hour, no second visit.
Command: build a pre-install checklist and a failure decision tree.
What do I check before I drive out. What do I do when phase 6 fails.
Format: numbered checklist, then a decision tree. One page max.
```

## 4. Billing that survives a webhook storm

```
Role: lead engineer on SAMePOS.
Context: Stripe webhooks must never double charge or double provision.
Inputs: BILLING.prisma, the Next.js webhook handler.
Constraints: idempotency required, WebhookEvent model exists.
Command: audit the handler for replay, out of order, and partial failure.
Give 3 hardening options with effort and risk. Pick one.
Format: findings table, then the patched handler file, then the test.
```

## 5. What ships next

```
Role: product engineer on SAMePOS.
Context: one live venue (Booth Liquor). I want venue number two.
Inputs: everything in this project.
Constraints: solo operator, limited hours, revenue is the goal.
Command: what are the 3 highest leverage things to build or fix
before I can sell to a second venue. Say what I can skip and why.
Format: table with impact, effort, and revenue link. End with this week's plan.
```
