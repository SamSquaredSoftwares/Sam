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

# Run the example agent (server must be up; needs ANTHROPIC_API_KEY)
python -m agents.snowflake_analyst "Which 5 customers spent the most?"
```

No linter or formatter is configured — there is no `pyproject.toml`, ruff, or
flake8 config in the repo, and no CI workflows. `pytest` is the only check.

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

### `query_snowflake`'s guard is a guardrail, not a boundary

`validate_read_only_sql` in `actions/snowflake_actions.py` tokenizes SQL rather
than pattern-matching it — it skips string literals, quoted identifiers, and
comments, so a semicolon inside `'a;b'` is data while `select 1; drop table t`
is refused, and it follows a `WITH`/`EXPLAIN` through to the statement it
actually runs. `tests/test_sql_guard.py` pins that behavior.

Treat it as usability protection only. The durable protection is a Snowflake
role holding just `USAGE`/`SELECT`, pointed at by `SNOWFLAKE_ROLE`. Don't
present the guard as a security control when writing docs or PR descriptions.

### Test layout

`conftest.py` at the repo root puts `actions/` on `sys.path`, so tests import
action modules directly (`from snowflake_actions import validate_read_only_sql`)
rather than as a package. Tests that load a module from a path — as
`tests/test_managed_agents.py` and `tests/test_snowflake_analyst.py` do — must
register it in `sys.modules` before `exec_module`, or `@dataclass` inside that
module fails to resolve its own module.

The agent tests stub both HTTP and the tool runner, so the whole suite runs with
no API key, no network, and no warehouse.

### `agents/` vs `managed-agents/` — easy to confuse

- `agents/` — agents built **with** this repo: Claude + the actions as tools.
- `managed-agents/` — Claude Code subagent definitions (`*.md`) used to work
  **on** this repo, plus a cross-platform installer that deploys them into the
  Claude Code managed-settings directory, where they override project- and
  user-level subagents of the same name.

### `db/` — SAMePOS licensing core, not yet reconciled

PostgreSQL licensing/trial-enforcement core for the SAMePOS node (migration
`024_licensing.sql` plus a standalone schema). `db/README.md` carries a
standing warning worth honoring: it has **not** been diffed against the
product's own migrations 001–023, and its `CREATE … IF NOT EXISTS` statements
would silently adopt a pre-existing object of the same name. It has only been
verified against a throwaway cluster, never a real node.
