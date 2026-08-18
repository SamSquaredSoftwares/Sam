# SAMePOS node install toolkit (remote driver)

Tooling for commissioning a SAMePOS venue node from a machine with **no PowerShell,
no SMB client and no remote desktop** — e.g. a Linux container driving an
internet-reachable venue box.

The authoritative runbook is the `samepos-node-install` skill. This directory does
not restate it; it provides the mechanics the runbook assumes you have.

| File | Purpose |
| --- | --- |
| `driver.py` | Credential proof + access diagnosis, SMB transfer, detached process launch, PID polling, log fetch. |
| `phase0_recon.ps1` | **Read-only** recon on the target: answers every prerequisite the runbook says to confirm rather than assume. |
| `tests/test_driver.py` | Unit tests for the driver's decisions (transport stubbed). |

## Why a driver rather than ad-hoc commands

Two constraints from the runbook shape everything here:

1. **Phases outlive the call that starts them.** `configure-node.ps1` runs 60–120s
   and many orchestration hosts cap a single call at ~60s. So every phase is
   launched *detached* via `Win32_Process.Create`, its PID polled in separate
   short calls, and its output read back from a log file afterwards. A process
   started as a child of a WinRM operation dies with that operation — which would
   kill a database build partway through.
2. **The admin share is not dependable.** On a token-filtered venue box `C$` keeps
   refusing even after `LocalAccountTokenFilterPolicy=1`. Payloads therefore go to
   a writable **non-admin** share, and elevated processes on the target write to
   `C:` themselves.

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install pywinrm impacket
export SAMEPOS_HOST=<ip-or-host>
export SAMEPOS_USER='VENUEPC\samadmin'
export SAMEPOS_PASS='...'          # environment only — never an argument
```

The password is read from the environment exclusively. Passed as a CLI argument it
would land in this shell's history and, once phases start, in the target's process
list.

Optional: `SAMEPOS_SMB_PORT` (445), `SAMEPOS_WINRM_PORT` (5985),
`SAMEPOS_WINRM_SCHEME` (http).

## Channels

`--channel cim` (default) uses DCOM/WMI. It works **before** WinRM is enabled, and
with `LocalAccountTokenFilterPolicy=1` it returns a full admin token, so phases run
elevated with no interactive UAC prompt. `--channel winrm` uses WS-Man and is
convenient for short synchronous probes.

## The one decision that gates everything

```bash
python3 driver.py probe
```

`probe` reports per-channel state plus a `verdict`. It exists to separate two
failures that look identical at a glance and lead to opposite actions:

- **`bad_credentials`** — SMB returned `STATUS_LOGON_FAILURE`. The password really
  is wrong. Go back to the venue.
- **`token_filtered`** — credentials *authenticate* (IPC$ succeeds) but `C$` and
  CIM return `STATUS_ACCESS_DENIED`. The credentials are correct; UAC remote-token
  filtering is stripping the admin token. Fix: run the skill's
  `scripts/enable-remoting.bat` **once interactively** (RDP/AnyDesk, one UAC
  approval), then re-probe. Do not re-check the password.

If `verdict.headless_install_possible` is true, the whole install can be driven
from here.

## Phase 0 — recon before anything is built

`phase0_recon.ps1` is strictly read-only: no database, no install, no service, no
venue data written. It is safe on a live trading box during service. It reports:

- identity and **actual elevation** obtained over the network;
- free space per drive, and whether `C:\SamePOS-Server` must be a junction onto a
  data drive (the product hardcodes that path in several places);
- the **DigitotPOS SQL instance and database for this venue** — these vary between
  venues (default instance vs `.\SQLEXPRESS`, `DigitotPOS_New` vs `DigitotPOS_psy`)
  and the importer must be pointed at the real ones;
- which of the four optional `articles` columns are **missing**, i.e. whether compat
  fix #2 is load-bearing here. This matters before the import, not after: the sync
  commits once at the very end, so one missing column rolls back the *entire*
  catalog/staff/tender import;
- an ODBC `SQL Server` driver, without which the import cannot connect at all;
- whether a SAMePOS install, its scheduled tasks, or ports 8077/5433 are already in
  use — i.e. whether this box is already commissioned.

```bash
python3 driver.py shares                      # pick a writable non-admin share
python3 driver.py put phase0_recon.ps1 --share E --dest 'samepos\phase0_recon.ps1'
python3 driver.py launch --script 'E:\samepos\phase0_recon.ps1' \
                         --log    'E:\samepos_logs\phase0.log'     # prints a PID
python3 driver.py poll <pid>                                       # RUNNING | GONE
python3 driver.py get --share E --path 'samepos_logs\phase0.log'   # the JSON report
```

Read `digitot_verdict` and `digitot_candidates` before building anything. If more
than one candidate database appears, **the venue must confirm which is live** — the
wrong choice imports a stale catalog into a trading system.

## Driving the remaining phases

Same launch/poll/fetch loop, one phase per launch, verifying each before the next:

```bash
python3 driver.py launch --script 'C:\SamePOS-Server\configure-node.ps1' \
                         --log    'E:\samepos_logs\configure.log'
```

Two ordering rules from the runbook that the driver cannot enforce for you:

- Apply the compat patches **before** `configure-node.ps1` runs the import.
- Re-apply them to the update bundle's `sync\` **before** `deploy-update.ps1`, which
  copies its own unpatched `sync\*.py` over yours.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

`.github/workflows/ci.yml` runs the same suite on every push and pull request,
plus a parse check over every `.ps1` in the repo. The parse check matters because
phase scripts are shipped to a live trading venue and run there, so a syntax
error otherwise costs a round trip against a real POS box.

44 tests, transport stubbed — they verify the decisions (error classification,
command construction and quoting, the access verdict, CLI wiring), not impacket.
The PowerShell is separately parse-checked with
`[System.Management.Automation.Language.Parser]::ParseFile`.
