# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Set up the local virtualenv (Python 3.12)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# Start the Action Server (OpenAPI + MCP on :8080; pass a port to change it)
./scripts/run_local.sh
./scripts/run_local.sh 9000

# Python tests
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_sql_guard.py -q            # one file
.venv/bin/python -m pytest tests/test_sql_guard.py -q -k stacked # one case

# Database tests — builds a throwaway PostgreSQL cluster in a temp dir.
# Needs initdb/pg_ctl on the box; exits with SKIP if they are absent.
db/tests/run_db_tests.sh

# Validate the managed subagent definitions (also covered by pytest)
python3 managed-agents/scripts/validate_agents.py

# Read-only Snowflake role. Printing is the default because these are
# privilege changes; --execute re-renders from the flags and environment it is
# given, so it prints what it will run and asks before applying. Pass the same
# flags to both, or the SQL you apply is not the SQL you reviewed. Both scripts
# read .env (see scripts/dotenv_file.py) as well as the environment.
.venv/bin/python scripts/snowflake_readonly_role.py \
    --role SAM_READONLY --user SAM_SERVICE \
    --warehouse COMPUTE_WH --database ANALYTICS
.venv/bin/python scripts/snowflake_readonly_role.py \
    --role SAM_READONLY --user SAM_SERVICE \
    --warehouse COMPUTE_WH --database ANALYTICS --execute
.venv/bin/python scripts/verify_snowflake_readonly.py

# Run the example agent (server must be up; needs ANTHROPIC_API_KEY)
.venv/bin/python -m agents.snowflake_analyst "Which 5 customers spent the most?"
```

No linter or formatter is configured — there is no `pyproject.toml`, ruff, or
flake8 config. `pytest` is the only check on the Python code.

CI (`.github/workflows/ci.yml`) runs this suite on **both 3.12 and 3.13** with
`fail-fast: false`, because the two interpreters catch different things: 3.12
is the floor `sema4ai-action-server>=3.2.0` will resolve on, and 3.13 turns on
`VERIFY_X509_STRICT` in `create_default_context()`, which is what catches
non-RFC-5280 certificate handling. It sets `SAM_TESTS_REQUIRE_TLS=1` so that a
TLS test skipping for a missing `cryptography` becomes a failure rather than a
silent green.

## Architecture

### The two layers, and why credentials never meet

The repo is deliberately split into a **tool layer** and a **brain layer**:

- `actions/` — Python functions decorated with `@action` (Sema4.ai). The Action
  Server discovers them and serves each one over **both** OpenAPI
  (`POST /api/actions/<package>/<action>/run`) and MCP (`/mcp`). This layer
  holds the Snowflake credentials.
- `agents/` — Claude agents that call those actions as tools. This layer holds
  only `ANTHROPIC_API_KEY`.

Nothing ever holds both. An agent asking for warehouse data goes
agent → HTTP → Action Server → Snowflake, so a prompt injection reaching the
agent cannot exfiltrate database credentials it never had. Preserve this split
when adding capabilities: put the credentialed work in an `@action`, not in the
agent process.

`docs/AI_AGENT_BLUEPRINT.md` is the long-form version of this design and the
guide the example agent implements.

### The action route segment is not `package.yaml`'s name

Action URLs are `/api/actions/<package>/<action>/run`, where `<package>` comes
from **the directory name the Action Server was pointed at** — not from
`name:` in `actions/package.yaml`. `scripts/run_local.sh` stages the action
`.py` files into `.devrun/sam-actions/`, which is the only reason the
documented URLs say `sam-actions`. A server started against another directory
serves another segment, and a hardcoded value 404s.

`agents/snowflake_analyst.py` therefore reads `/openapi.json` at startup and
discovers the segment (`discover_package`), which doubles as its reachability
check. Do the same in any new agent rather than hardcoding.

### Two dependency manifests that must stay in sync

- `requirements.txt` — the local virtualenv used by `run_local.sh` and tests.
- `actions/package.yaml` — the RCC-managed environment used in production.

`tests/test_dependency_manifests.py` fails if a package pinned in both stops
agreeing, which is the only automated thing standing between the two.

`run_local.sh` stages the actions **without** `package.yaml` on purpose: that
makes the server run in unmanaged mode, skipping the RCC bootstrap, which needs
network access to `cdn.sema4.ai` and fails in sandboxed environments. A
dependency added to one manifest and not the other will work locally and break
in production, or vice versa.

Watch what `anthropic` drags in: since 1.0 it depends on **`httpx2`**, not
`httpx`. `agents/` import plain `httpx`, so `requirements.txt` declares it
explicitly — before that it only arrived transitively through
`snowflake-connector-python`, and any environment without the Snowflake
connector failed at import. Note also that the agent uses the SDK's *beta* tool
runner (`anthropic.beta_tool`, `client.beta.messages.tool_runner`), which is
verified against 1.0.0 but is not a stable API surface.

That split has already broken this repo once: `tls_trust.anthropic_http_client()`
used to build an `httpx.Client`, which `anthropic` 1.0 rejects with a
`TypeError`, silently disabling custom CA bundles on the Anthropic path. It is
fixed — the client is built on the SDK's `httpx2` boundary and `requirements.txt`
caps the SDK at `anthropic>=1,<2` — and the CI job added alongside exists
because nothing was running these tests when it slipped through. When touching
either HTTP client, check which of the two libraries you are handing to whom.

### Outbound TLS: two clients that disagree about CA bundles

`actions/tls_trust.py` exists because the package's two HTTPS clients resolve
trust differently, which matters on any network that re-terminates TLS (an
inspecting proxy, a sandboxed CI runner):

- `httpx` (the Anthropic SDK) reads `SSL_CERT_FILE` / `SSL_CERT_DIR` and
  **ignores `REQUESTS_CA_BUNDLE` entirely**.
- The Snowflake connector resolves `ca_certs` kwarg → `REQUESTS_CA_BUNDLE` →
  `SSL_CERT_FILE`, then falls back to `certifi`. `ca_certs` is a socket-level
  argument, not a `connect()` parameter, so env vars are the only route.

`SSL_CERT_FILE` is the one name both honor; `SAM_CA_BUNDLE` wins over both.
The module also converts a mistyped bundle path into a loud error — the
Snowflake connector otherwise swallows it in a `try/except ... pass` and fails
later with a message that never mentions the typo.

It deliberately has no "insecure" / `verify=False` switch. The fix for an
interception proxy is to trust its CA, never to stop verifying — don't add one.

### `query_snowflake`'s guard is a guardrail, not a boundary

`validate_read_only_sql` in `actions/snowflake_actions.py` tokenizes SQL rather
than pattern-matching it — it skips string literals, quoted identifiers, and
comments, so a semicolon inside `'a;b'` is data while `select 1; drop table t`
is refused, and it follows a `WITH`/`EXPLAIN` through to the statement it
actually runs. `tests/test_sql_guard.py` pins that behavior.

Treat it as usability protection only. The boundary is the Snowflake role in
`snowflake/readonly_role.sql` — `USAGE`/`SELECT` and nothing else — rendered
and applied by `scripts/snowflake_readonly_role.py` and proven by
`scripts/verify_snowflake_readonly.py`, with `SNOWFLAKE_ROLE` pointing the
actions at it. Don't present the guard as a security control when writing docs
or PR descriptions; the role is the thing that makes the claim true.

The verifier is what makes that claim checkable, and two of its properties are
load-bearing. **A write probe has to target an object that exists**: Snowflake
answers a write against a missing table with `does not exist or not
authorized`, which is the same answer a role with full write access gets, so it
proves nothing. The DML probes therefore run against a real table found through
`INFORMATION_SCHEMA` (or `--probe-table`), written to touch zero rows inside a
rolled-back transaction so they stay harmless either way. And **"not proven
safe" is a failure**: only an explicit privilege refusal passes, so a probe that
fails for any other reason exits non-zero rather than printing a warning.
`tests/test_verify_snowflake_readonly.py` pins both against Snowflake's real
error strings.

An earlier version had neither property — its privilege markers included
Snowflake's object-not-found text and every DML probe targeted a table that by
design did not exist — so it reported "Safe to use as SNOWFLAKE_ROLE" for a role
holding `INSERT`/`UPDATE`/`DELETE`. If you change the markers, keep the
not-found test ahead of the privilege test: the not-found message ends in the
words "not authorized".

The verifier also reads `SHOW GRANTS TO ROLE`, and fails on any privilege
outside `USAGE`/`SELECT`-and-friends or any inherited role. That covers what a
`DROP` probe cannot: dropping a real table to see whether it is possible is not
a test anyone can run twice.

### Test layout

`conftest.py` at the repo root puts `actions/` on `sys.path`, so tests import
action modules directly (`from snowflake_actions import validate_read_only_sql`)
rather than as a package. Tests that load a module from a path — as
`tests/test_managed_agents.py` and `tests/test_snowflake_analyst.py` do — must
register it in `sys.modules` before `exec_module`, or `@dataclass` inside that
module fails to resolve its own module.

The agent tests stub both HTTP and the tool runner, so the whole suite runs with
no API key, no network, and no warehouse.

### Three agent-shaped directories — easy to confuse

- `agents/` — agents built **with** this repo: Claude + the actions as tools.
- `managed-agents/` — Claude Code subagent definitions (`*.md`) used to work
  **on** this repo, plus a cross-platform installer that deploys them into the
  Claude Code managed-settings directory, where they override project- and
  user-level subagents of the same name.
- `docs/samepos-project/` — the version-controlled source for a **claude.ai
  Project** (custom instructions, knowledge files, starter prompts) about the
  SAMePOS product. Nothing here executes; it is edited, committed, then
  re-uploaded to the Project by hand.

None of the three substitutes for another, and a change in one does not
propagate to the others.

### `.env` is read literally, in two places that must agree

`.env` holds a Snowflake password, which is arbitrary text. Nothing sources it
as shell — `. .env` turns `Sn0w$Flake!2026` into `Sn0w!2026` and aborts
outright on a value containing a space. Two readers implement the same small
grammar, because `run_local.sh` needs `.env` before the virtualenv it would
need to run Python exists:

- `scripts/dotenv_file.py` — used by the two `scripts/*snowflake*` scripts.
- `scripts/load_dotenv.sh` — sourced by `run_local.sh`.

`tests/test_dotenv_file.py` runs both over the same fixtures and fails if they
diverge. Values already in the environment win in both. Change one, change the
other.

### The Claude Code run config

`.claude/settings.json` is committed and shared; `.claude/settings.local.json`
is per-developer and gitignored. The `SessionStart` hook
(`.claude/hooks/setup-venv.sh`) builds `.venv` on the first session in a fresh
clone and is a no-op afterwards, so `.venv/bin/python -m pytest tests/ -q`
works from the first prompt.

`Read` is pre-approved, so the `deny` list is what keeps `.env` — a live
Snowflake password and Anthropic key — out of a transcript. Anything else
carrying real credentials belongs there too.

### `db/` — SAMePOS licensing core, not yet reconciled

PostgreSQL licensing/trial-enforcement core for the SAMePOS node (migration
`024_licensing.sql` plus a standalone schema). `db/README.md` carries a
standing warning worth honoring: it has **not** been diffed against the
product's own migrations 001–023, and its `CREATE … IF NOT EXISTS` statements
would silently adopt a pre-existing object of the same name. It has only been
verified against a throwaway cluster, never a real node.
