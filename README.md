# Sam

A [Sema4.ai](https://sema4.ai) Action Package plus an [Action Server](https://github.com/Sema4AI/actions/tree/master/action_server) setup for serving AI actions over HTTP (OpenAPI) and MCP.

## Layout

```
actions/            The action package
  package.yaml      Package metadata + managed-environment dependencies (RCC)
  actions.py        The @action definitions
  snowflake_actions.py  The Snowflake @action definitions
agents/
  snowflake_analyst.py  Claude agent that answers questions via the actions
scripts/
  run_local.sh      Start the Action Server in unmanaged mode (no RCC needed)
docs/
  AI_AGENT_BLUEPRINT.md  How to build an AI agent on top of these actions
db/                 Licensing schema, migrations, and SQL behavior tests
managed-agents/     Claude Code managed subagents + cross-platform installer
tests/              Python test suite
requirements.txt    Dependencies for the local virtualenv
```

## Documentation

- **[The AI Agent Blueprint](docs/AI_AGENT_BLUEPRINT.md)** — how to build an AI
  agent on top of this repo: deciding whether a task needs an agent at all,
  choosing among the four build approaches, designing the tool surface from
  these actions, a runnable Snowflake analyst agent, and production hardening
  (secrets, error handling, prompt caching, context management, evals).
- [`managed-agents/README.md`](managed-agents/README.md) — the Claude Code
  managed subagents used to work *on* this repo, and how to install them.
  (Distinct from the agents the blueprint teaches you to build *with* this repo.)
- [`db/README.md`](db/README.md) — licensing schema, migrations, and how to run
  the database tests.

## Actions

| Action                      | Description                                                              |
| --------------------------- | ------------------------------------------------------------------------ |
| `health_check`              | No-credential smoke test; reports the Python/runtime the actions run on. |
| `ask_claude`                | Sends a prompt to Claude (Anthropic API) and returns the text reply.     |
| `test_snowflake_connection` | Connects to Snowflake and reports version/session context.               |
| `query_snowflake`           | Runs a read-only SQL statement and returns rows as JSON (max 1000 rows). |

`ask_claude` takes its API key as a Sema4.ai `Secret` (passed out of band by the
Action Server, e.g. via the `x-action-context` header) and falls back to the
`ANTHROPIC_API_KEY` environment variable.

## Quick start

```bash
./scripts/run_local.sh          # serves on http://localhost:8080
./scripts/run_local.sh 9000     # custom port
```

This creates `.venv/`, installs `requirements.txt`, and starts the Action
Server in **unmanaged mode** (the actions run in the same virtualenv as the
server). This mode avoids the RCC bootstrap, which needs network access to
`cdn.sema4.ai` and is therefore the right choice for restricted/sandboxed
environments.

Once running:

- Web UI / OpenAPI: `http://localhost:8080` (spec at `/openapi.json`)
- MCP endpoint: `http://localhost:8080/mcp`
- Run an action:

```bash
curl -X POST http://localhost:8080/api/actions/sam-actions/health-check/run \
  -H 'Content-Type: application/json' -d '{}'

curl -X POST http://localhost:8080/api/actions/sam-actions/ask-claude/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Say hello"}'
```

Set `ANTHROPIC_API_KEY` in the server's environment (or pass the `api_key`
secret through the action context) before calling `ask_claude`.

## Running an agent

With the server up, `agents/snowflake_analyst.py` answers questions by letting
Claude query the warehouse through the Snowflake actions:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m agents.snowflake_analyst "Which 5 customers generated the most revenue last quarter?"
```

It discovers the action package from the server's OpenAPI spec; use `--server`
(or `SAM_ACTION_SERVER`) to point it elsewhere, and `--effort low` to trade
depth for speed. The Snowflake credentials stay with the Action Server — the
agent process only ever sees `ANTHROPIC_API_KEY`. See
[The AI Agent Blueprint](docs/AI_AGENT_BLUEPRINT.md) for how it is built.

## Snowflake configuration

The Snowflake actions read their connection settings from Sema4.ai secrets
(passed out of band by the Action Server) with environment-variable fallbacks:

| Variable                           | Required | Notes                                        |
| ---------------------------------- | -------- | -------------------------------------------- |
| `SNOWFLAKE_ACCOUNT`                | yes      | Account identifier, e.g. `myorg-myaccount`   |
| `SNOWFLAKE_USER`                   | yes      |                                              |
| `SNOWFLAKE_PASSWORD`               | yes*     | *Or use key-pair auth below                  |
| `SNOWFLAKE_PRIVATE_KEY_PATH`       | no       | Path to a PKCS#8 private key (key-pair auth) |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | no       | Passphrase for an encrypted private key      |
| `SNOWFLAKE_WAREHOUSE`              | no       |                                              |
| `SNOWFLAKE_DATABASE`               | no       |                                              |
| `SNOWFLAKE_SCHEMA`                 | no       |                                              |
| `SNOWFLAKE_ROLE`                   | no       |                                              |

The easiest way to configure credentials locally is a `.env` file, which
`scripts/run_local.sh` loads automatically:

```bash
cp .env.example .env
# edit .env and fill in SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD
# (and ANTHROPIC_API_KEY for ask_claude)
./scripts/run_local.sh
```

`.env` is gitignored — never commit real credentials. In production, provide
the same values as Sema4.ai secrets or environment variables on the server.

Example using plain environment variables instead:

```bash
export SNOWFLAKE_ACCOUNT=myorg-myaccount
export SNOWFLAKE_USER=sam
export SNOWFLAKE_PASSWORD=...
export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
./scripts/run_local.sh

curl -X POST http://localhost:8080/api/actions/sam-actions/test-snowflake-connection/run \
  -H 'Content-Type: application/json' -d '{}'

curl -X POST http://localhost:8080/api/actions/sam-actions/query-snowflake/run \
  -H 'Content-Type: application/json' \
  -d '{"sql": "select current_timestamp()", "max_rows": 10}'
```

`query_snowflake` only accepts a single read-only statement
(SELECT/SHOW/DESCRIBE/WITH/EXPLAIN) and caps results at 1000 rows.

## Managed environments (production)

On a machine with normal network access, the standard Sema4.ai flow works
directly — `package.yaml` declares the runtime for RCC to build:

```bash
pip install sema4ai-action-server
cd actions
action-server start   # bootstraps the RCC-managed environment automatically
```

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```
