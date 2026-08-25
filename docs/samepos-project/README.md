# SAMePOS Claude Project: Setup Pack

The version-controlled source for the SAMePOS Claude Project.
Built for Mr LSG, Sam Squared Softwares. First committed 25 Aug 2026.

A Claude Project is a kitchen. The custom instructions are the house
rules. The knowledge files are the stocked pantry. Miss either one and
every dish comes out different.

This directory keeps both out of chat threads and in git, so they can
never be lost again. Edit here, commit, then re-upload to the project.

## DO THIS FIRST (5 minutes, highest value)

The SAMePOS cash-up logic, stock control schemas, and provider configs
may only exist inside the OLD Claude Project chats. Not in code. Not in
memory. Just in old threads. That is a single point of failure.

Open the old project. Paste this:

```
Export everything you know about SAMePOS that is NOT already in a code file.
Cover: cash-up rules and edge cases, stock control logic, loss event rules,
VAT handling, sync conflict rules, and any provider config decisions.
Output as one markdown document. Use placeholders like <SUPABASE_SERVICE_KEY>
for every secret. Do not print real keys.
```

Merge the output into `knowledge/CASHUP_AND_STOCK_RULES.md` (a starting
draft already exists there, built from this repo). Commit it.

**If the building burns down, this is the file you carry out.**

## What is in this directory

| File | What it is |
|------|-----------|
| `CUSTOM_INSTRUCTIONS.md` | The block to paste into the project's Custom Instructions setting |
| `STARTER_PROMPTS.md` | Five prompts to run once the files are uploaded |
| `knowledge/` | The ten knowledge files to upload to the project |

## Knowledge file status

Ten files. Their state as of this commit:

| # | File | State | Your action |
|---|------|-------|-------------|
| 1 | `CASHUP_AND_STOCK_RULES.md` | DRAFT from this repo | Run the rescue prompt above, merge, correct |
| 2 | `SCHEMA.sql` | PLACEHOLDER | Restore the paused Supabase project, run the pg_dump command inside the file |
| 3 | `ARCHITECTURE.md` | DRAFT | Read once, correct anything wrong, commit |
| 4 | `SYNC_ENGINE.md` | PLACEHOLDER | Upload the real `SYNC_ENGINE.py` from the product repo instead |
| 5 | `ENV_TEMPLATE.md` | DRAFT | Diff against your real `.env` files, add anything missing |
| 6 | `INSTALLER_RUNBOOK.md` | DRAFT from this repo | Add the ten phase detail and field notes from the installer README |
| 7 | `BILLING.prisma` | DRAFT | Replace with the real Prisma schema file from the billing repo |
| 8 | `TEST_PLAN.md` | DRAFT | Trim to what you will actually run |
| 9 | `BOOTH_LIQUOR_NOTES.md` | TEMPLATE | Write 10 bullets from memory. Takes 10 minutes |
| 10 | `DECISIONS.md` | SEEDED from this repo | Add one line per decision as you make them |

DRAFT means Claude wrote it from the setup pack plus this repo's real
code and docs. Every drafted file marks which claims are verified
against this repo and which need your confirmation.

**Rule for every upload: placeholders for secrets. Always. A knowledge
file is forever.**

## How to run the project

Three ways. Pick one.

| Option | What it is | Effort to set up | Best when |
|--------|-----------|------------------|-----------|
| Lean | Custom instructions plus files 1, 2, 5 | 30 minutes | You want value today |
| Standard | Files 1 to 7, plus a weekly decisions update | 2 hours | You are building most days |
| Full | All 10 files plus a daily bug test prompt on schedule | Half a day | You are onboarding venue two |

Pick: Lean today, Standard by Friday. A half-stocked pantry you cook
from beats a perfect one you never open. File 2 alone (the schema)
removes most of the wrong answers. Get files 1, 2, and 5 in, run
starter prompt 1, and let the real gaps tell you what to add next.

## Keeping this in sync

1. Edit the file here, not in the Claude Project.
2. Commit and push.
3. Re-upload the changed file to the project (delete the old copy first).

## Your next action

Open the old SAMePOS project. Paste the rescue prompt. Save the output.

**That one file is worth more than the other nine combined. Go get it.**
