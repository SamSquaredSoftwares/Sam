# Sam

A [Sema4.ai](https://sema4.ai) Action Package plus an [Action Server](https://github.com/Sema4AI/actions/tree/master/action_server) setup for serving AI actions over HTTP (OpenAPI) and MCP.

## Layout

```
actions/            The action package
  package.yaml      Package metadata + managed-environment dependencies (RCC)
  actions.py        The @action definitions
scripts/
  run_local.sh      Start the Action Server in unmanaged mode (no RCC needed)
requirements.txt    Dependencies for the local virtualenv
```

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
