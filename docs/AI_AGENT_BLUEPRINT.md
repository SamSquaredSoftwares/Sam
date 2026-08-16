# The AI Agent Blueprint

How to build an AI agent on top of this repository — from "should this even be
an agent?" to a production-hardened loop.

This repo already contains half of an agent: the **tool layer**. The Action
Server exposes every `@action` in `actions/` over HTTP (OpenAPI) and MCP, with
secrets handled out of band and a read-only guard on SQL. What's missing is the
**brain**: the loop that lets Claude decide which tools to call, in what order,
until the job is done. This blueprint adds that half, then hardens it.

```
                 ┌──────────────────────────────────────────────┐
                 │                 Agent host                    │
                 │  (your Python process — the loop lives here)  │
                 │                                               │
   user goal ───▶│   system prompt + conversation history        │
                 │        │                        ▲             │
                 │        ▼                        │             │
                 │   Claude (claude-opus-5)   tool results       │
                 │        │                        │             │
                 │   tool_use blocks ──── execute ─┘             │
                 └───────────────│──────────────────────────────┘
                                 ▼  HTTP / MCP
                 ┌──────────────────────────────────────────────┐
                 │            Sema4.ai Action Server             │
                 │   actions/actions.py, snowflake_actions.py    │
                 │   • OpenAPI:  POST /api/actions/.../run       │
                 │   • MCP:      http://localhost:8080/mcp       │
                 │   • Secrets:  x-action-context / env vars     │
                 └───────────────│──────────────────────────────┘
                                 ▼
                        Snowflake, Anthropic API, …
```

An agent is just this diagram in motion: **a model, a set of tools, a loop
that feeds tool results back to the model, and the context that accumulates
along the way.** Everything else in this document is detail on those four
parts.

---

## Step 0 — Should this be an agent at all?

Agents cost more, run longer, and fail in more interesting ways than a single
API call. Check all four before reaching for the agent tier:

| Criterion | Question to ask |
| --- | --- |
| **Complexity** | Is the task multi-step and hard to fully specify in advance? ("Investigate why revenue dipped in March" vs. "run this exact query") |
| **Value** | Does the outcome justify higher cost and latency? |
| **Viability** | Is Claude actually capable at this task type? |
| **Cost of error** | Can mistakes be caught and recovered from (read-only tools, review steps, rollbacks)? |

If any answer is "no", stay at a simpler tier:

- **Single call** — classification, summarization, extraction, Q&A. That is
  what the existing `ask_claude` action already does.
- **Workflow** — a fixed pipeline where *your code* decides the steps and
  Claude fills in judgment at specific points. Deterministic steps
  (routing, tallying, formatting) belong in plain code, not in a loop.
- **Agent** — open-ended, model-driven tool use. The rest of this blueprint.

## Step 1 — Pick a build approach

There are four ways to get an agent. Two questions separate them: who supplies
the **harness** (the loop + context management), and who supplies the
**deployment** (the infrastructure it runs on).

| # | Approach | You write | Harness / deployment | Best when |
| --- | --- | --- | --- | --- |
| 1 | **Manual loop** (Claude API) | The `while stop_reason == "tool_use"` loop yourself | You / you | You need to own the entire loop — custom transport, no beta dependency |
| 2 | **SDK Tool Runner** (Claude API, beta) | Just the tool functions | SDK / you | A custom-tool agent without hand-writing the loop — **the right default for this repo** |
| 3 | **Managed Agents** (REST, beta) | Agent config + tool results | Anthropic / Anthropic | You want Anthropic to run the loop *and* host a per-session sandbox |
| 4 | **Claude Agent SDK** | A prompt + options | SDK (full Claude Code harness) / you | You want a batteries-included coding/filesystem agent |

This blueprint uses **approach 2**: the actions in this repo are the tools,
and the Anthropic SDK's tool runner drives the loop. Approach 1 is shown as an
alternative in Step 3; approaches 3 and 4 are pointers in "Scaling up" at the
end.

## Step 2 — Design the tool surface

Your agent is only as good as its tools. This repo's `@action` pattern already
encodes the important rules — copy it when adding tools.

**What a good tool looks like** (from `actions/snowflake_actions.py`):

- **A clear, specific name.** `query_snowflake`, not `db`.
- **A docstring that says *when* to use it, not just what it does.** The
  Action Server turns the docstring into the tool description, and Claude
  relies on descriptions to decide when to call. Trigger conditions ("call
  this when the user asks about current data") measurably improve tool
  selection.
- **Typed, described parameters** with sensible defaults (`max_rows: int = 100`).
- **A security boundary enforced in code, not in the prompt.**
  `query_snowflake` rejects anything that isn't a single
  SELECT/SHOW/DESCRIBE/WITH/EXPLAIN statement and caps rows at 1000. The
  prompt can *ask* Claude to be careful; the tool *guarantees* it. Never rely
  on the model to enforce a boundary the code can enforce.
- **Secrets out of band.** Credentials arrive as Sema4.ai `Secret` params
  (via the `x-action-context` header) or environment variables — never in the
  request body, never in the conversation. A secret pasted into a prompt is
  persisted in conversation history forever.
- **Errors as data.** Raising `ActionError` with an informative message lets
  the agent read the failure and adapt ("missing SNOWFLAKE_ACCOUNT" → tell
  the user, don't retry blindly).

**Rule of thumb for choosing tools:** start broad (a bash-like or generic
query tool gives leverage), then promote an action to its own dedicated tool
whenever you need to gate it, audit it, render it specially, or run it in
parallel safely. Hard-to-reverse operations (writes, sends, deletes) deserve
dedicated tools with tight schemas — or shouldn't exist at all, which is why
this repo's SQL tool is read-only.

**Adding a new tool to the surface** is just another action:

```python
# actions/actions.py
@action
def summarize_table(table: str) -> str:
    """Describe a Snowflake table: columns, types, and row count.

    Use this before writing queries against an unfamiliar table.

    Args:
        table: Fully qualified table name, e.g. "ANALYTICS.PUBLIC.ORDERS".

    Returns:
        A human-readable schema summary.
    """
    ...
```

Restart `./scripts/run_local.sh` and it is live on both the OpenAPI and MCP
surfaces — no agent-side changes needed until you wire it in.

## Step 3 — Build the loop

Below is a complete, runnable agent: a **Snowflake analyst** that answers
questions by querying the warehouse through this repo's Action Server.

Prerequisites:

```bash
./scripts/run_local.sh            # Action Server on http://localhost:8080
# .env (loaded by run_local.sh) provides SNOWFLAKE_* credentials
# ANTHROPIC_API_KEY must be set in the agent's environment
```

Save as `agents/snowflake_analyst.py` (the `anthropic` package is already in
`requirements.txt`, and `httpx` ships with it — no new dependencies):

```python
"""A Snowflake analyst agent built on the Sam Action Server.

The Action Server is the tool layer; this file is the brain. Each tool
function wraps one action's OpenAPI run endpoint, and the Anthropic SDK's
tool runner drives the loop until Claude has an answer.
"""

import json
import sys

import anthropic
import httpx
from anthropic import beta_tool

ACTION_SERVER = "http://localhost:8080"
MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are a data analyst working against a Snowflake warehouse.

Answer the user's question by querying the warehouse with the run_sql tool.
Explore before you assert: when a table is unfamiliar, look at its columns
(DESCRIBE TABLE, or a small SELECT) before writing the real query. Prefer a
few small, targeted queries over one giant one, and keep result sets small —
you rarely need more than 100 rows to answer a question.

Ground every claim in query results from this session. If the data cannot
answer the question, say so plainly instead of guessing. When you are done,
lead with the answer, then show the key numbers that support it."""


def _run_action(name: str, payload: dict) -> str:
    """Call one action's OpenAPI run endpoint and return its result as text."""
    resp = httpx.post(
        f"{ACTION_SERVER}/api/actions/sam-actions/{name}/run",
        json=payload,
        timeout=120.0,
    )
    if resp.status_code != 200:
        # Return the error as text so Claude can read it and adapt.
        return f"Action failed (HTTP {resp.status_code}): {resp.text}"
    data = resp.json()  # actions return strings; the server JSON-encodes them
    return data if isinstance(data, str) else json.dumps(data)


@beta_tool
def run_sql(sql: str, max_rows: int = 100) -> str:
    """Run a single read-only SQL statement against Snowflake.

    Only SELECT / SHOW / DESCRIBE / WITH / EXPLAIN statements are accepted;
    anything else is rejected by the server. Results are capped at 1000 rows.

    Args:
        sql: The SQL statement to run (no trailing semicolon needed).
        max_rows: Maximum rows to return. Keep this small — the result is
            returned as JSON into the conversation.

    Returns:
        JSON with `columns`, `rows`, `row_count`, and `truncated`.
    """
    return _run_action("query-snowflake", {"sql": sql, "max_rows": max_rows})


@beta_tool
def check_connection() -> str:
    """Verify Snowflake connectivity and report the session context.

    Use this once at the start if a query fails with a connection error,
    to distinguish configuration problems from SQL problems.

    Returns:
        The Snowflake version and current account/role/warehouse/database.
    """
    return _run_action("test-snowflake-connection", {})


def main() -> None:
    question = " ".join(sys.argv[1:]) or "What tables can you see, and how big are they?"
    client = anthropic.Anthropic()

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[run_sql, check_connection],
        messages=[{"role": "user", "content": question}],
    )

    final = None
    for message in runner:  # one iteration per model turn; tools run in between
        final = message
        for block in message.content:
            if block.type == "text":
                print(block.text, flush=True)

    if final is not None and final.stop_reason == "refusal":
        print("The model declined this request.", file=sys.stderr)


if __name__ == "__main__":
    main()
```

Run it:

```bash
python agents/snowflake_analyst.py "Which 5 customers generated the most revenue last quarter?"
```

What the tool runner does for you: it sends the request, executes your tool
functions when Claude asks for them, feeds the results back, and stops when
Claude stops calling tools. Tool schemas are generated from the function
signatures and docstrings — the same convention this repo's actions already
follow. It is not a black box: each loop iteration yields the assistant
message *before* the tools run, so you can log tool calls, gate a dangerous
one behind human approval, or modify a result before it goes back.

### The manual loop (when you need to own everything)

Use this only when the runner's hooks aren't enough (custom transport,
avoiding the beta dependency). The shape every agent reduces to:

```python
messages = [{"role": "user", "content": question}]
while True:
    response = client.messages.create(
        model=MODEL, max_tokens=16000, system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS, messages=messages,
    )
    if response.stop_reason == "pause_turn":      # server-side tool paused; resume
        messages.append({"role": "assistant", "content": response.content})
        continue
    if response.stop_reason != "tool_use":        # end_turn, refusal, max_tokens
        break
    messages.append({"role": "assistant", "content": response.content})
    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = execute(block.name, block.input)   # your dispatch
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
    messages.append({"role": "user", "content": results})   # ALL results, ONE message
```

Rules that keep the manual loop correct:

- Append the **full** `response.content` (it contains the `tool_use` blocks),
  not just the text.
- Claude may request several tools in one turn. Execute them all and return
  **all** `tool_result` blocks in a **single** user message — splitting them
  across messages trains the model to stop parallelizing.
- A failed tool gets a `tool_result` with `"is_error": True` — never drop it.
- Parse `block.input` as structured data; never string-match the raw JSON.
- Bound the loop (`max_iterations`) so a confused agent can't spin forever.

### Model and parameters

- **Model:** `claude-opus-5` — the default for agentic work. Drop to
  `claude-haiku-4-5` for simple, high-volume tool routing; the repo's
  `ask_claude` default (`claude-sonnet-5`) is the middle ground.
- **Thinking:** on `claude-opus-5` thinking is on by default — omit the
  `thinking` parameter. Don't disable it for an agent; if cost matters, lower
  effort instead.
- **Effort:** `output_config={"effort": "..."}` is the cost/quality lever.
  `high` is the default; `xhigh` is the sweet spot for hard agentic work;
  `low`/`medium` are strong on routine tasks. Don't set `temperature` /
  `top_p` / `top_k` — they are rejected on `claude-opus-5`.
- **max_tokens:** ~16000 for non-streaming. Above that, stream
  (`client.messages.stream(...)` + `get_final_message()`) to avoid HTTP
  timeouts.

## Step 4 — Structured output when code consumes the answer

When the agent's final answer feeds another program (a dashboard, a ticket, a
row in a table), don't parse prose — constrain the output:

```python
from pydantic import BaseModel

class AnalysisResult(BaseModel):
    answer: str
    sql_used: list[str]
    caveats: list[str]

response = client.messages.parse(
    model=MODEL,
    max_tokens=16000,
    messages=[{"role": "user", "content": summary_request}],
    output_format=AnalysisResult,
)
result = response.parsed_output   # a validated AnalysisResult
```

On raw `messages.create()` the equivalent is
`output_config={"format": {"type": "json_schema", "schema": {...}}}`. For tool
*inputs*, add `strict: true` to a tool definition (with
`additionalProperties: false` and `required`) to guarantee the arguments
validate exactly. Do not use assistant-message prefills to force JSON — they
return a 400 on current models.

## Step 5 — Production hardening

### Secrets

- Agent-side: only `ANTHROPIC_API_KEY`, from the environment.
- Tool-side: Snowflake credentials stay in the Action Server (Sema4.ai
  `Secret` params / `.env` / env vars, per the README). The agent process
  never sees them, so a prompt injection in the conversation cannot leak them.
- Never put a credential in a system prompt, user message, or tool result —
  conversation history is persisted and replayed.

### Error handling

Catch the SDK's typed exceptions most-specific-first; retryable (429, 5xx,
network) and non-retryable (4xx) failures need different treatment:

```python
try:
    ...  # runner / messages.create
except anthropic.RateLimitError:
    ...  # back off and retry (the SDK already retries twice by default)
except anthropic.APIStatusError as e:
    ...  # non-retryable API error — log e.status_code, e.message
except anthropic.APIConnectionError:
    ...  # network — retry with backoff
```

And handle the stop reasons that aren't `end_turn`:

| `stop_reason` | Meaning | Response |
| --- | --- | --- |
| `tool_use` | Claude wants a tool | Execute and continue (the runner does this) |
| `pause_turn` | Server-side tool paused mid-turn | Re-send and resume |
| `max_tokens` | Output truncated | Raise `max_tokens` or stream |
| `refusal` | Safety classifiers declined | Surface it; don't blind-retry the same prompt |

Check `stop_reason` **before** reading `response.content` — on a refusal the
content can be empty, and `content[0]` will raise.

### Prompt caching

An agent re-sends its whole history every turn; caching makes that cheap.
Caching is a **prefix match** — order the request stable-first:

1. Tools (deterministic order — don't rebuild the list per request)
2. System prompt (frozen — no timestamps, no per-user interpolation)
3. Conversation history (grows at the end)

```python
system=[{"type": "text", "text": SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}}],
```

Verify with `response.usage.cache_read_input_tokens` — zero across repeated
turns means something volatile (a timestamp, unsorted JSON, a changing tool
set) is invalidating the prefix.

### Context management for long runs

| Pattern | Use when | What it does |
| --- | --- | --- |
| **Context editing** (beta) | Old tool results are stale | Clears old tool results/thinking from the transcript |
| **Compaction** (beta) | Conversation nears the context window | Server-side summary replaces earlier history |
| **Memory** (file-based) | State must survive across sessions | Claude reads/writes a memory directory you host |

Also: keep tool results small at the source — `query_snowflake`'s
`max_rows` cap is context management. A 100-row JSON answer beats a
100,000-row dump the model has to wade through every subsequent turn.

### Cost and observability

- Log `response.usage` (input/output/cache tokens) and `response._request_id`
  per turn — the request ID is what Anthropic support needs.
- Tune effort per route: an agent that verifies its own work at `xhigh` may
  cost less overall than a sloppy `low` run that needs three retries.

## Step 6 — Evaluate, then iterate

Before shipping, build a small eval set — 10–20 real questions with known-good
answers — and score three things separately:

1. **Tool selection** — did it call the right tools (and not call tools when
   it shouldn't)?
2. **Groundedness** — is every claim in the answer supported by a query
   result from the session?
3. **Outcome** — is the final answer correct?

Iterate on the *system prompt and tool descriptions*, not on hope. Current
models follow instructions literally: if the agent under-uses a tool, state
when to use it in that tool's docstring; if it over-explains, say what a good
answer looks like. Dial back any "CRITICAL: you MUST…" language — it causes
over-triggering on current models.

## Scaling up

- **Many tools?** Past a few dozen, don't load every schema every turn — use
  tool search / deferred loading, or split into multiple specialist agents.
- **Need hosting, scheduling, or sandboxed execution?** Managed Agents runs
  the loop and a per-session container on Anthropic's side; your actions
  remain reachable as MCP tools (`mcp_servers` + `mcp_toolset` pointing at
  this server's `/mcp` endpoint) or as custom tools your host answers.
- **MCP instead of OpenAPI?** This server already speaks MCP at
  `http://localhost:8080/mcp`. The Anthropic SDK's MCP helpers
  (`pip install "anthropic[mcp]"`, then `async_mcp_tool`) can convert the
  server's MCP tools straight into tool-runner tools — one integration that
  automatically picks up every new action, at the cost of losing the explicit
  per-tool wrappers shown above.
- **Multiple agents?** Give each a narrow system prompt and tool set; a
  coordinator delegates. Resist this until a single agent demonstrably runs
  out of context or focus — every delegation costs a re-briefing.

## The checklist

- [ ] Passed the Step 0 gate — this genuinely needs an agent
- [ ] Tool surface designed: specific names, when-to-use docstrings,
      code-enforced boundaries (read-only SQL, row caps), `ActionError` for
      failures
- [ ] Secrets flow through the Action Server / environment — never prompts
- [ ] Loop built on the tool runner (or a bounded manual loop with full
      content appended and all tool results in one message)
- [ ] `claude-opus-5`, thinking left on, effort tuned, no sampling params
- [ ] `stop_reason` handled: `refusal`, `pause_turn`, `max_tokens`
- [ ] Typed exception handling, SDK retries left on
- [ ] System prompt frozen + `cache_control` set; cache reads verified
- [ ] Tool results bounded at the source; context strategy for long runs
- [ ] Eval set exists; tool selection, groundedness, and outcomes measured

## Reference: what lives where

| Piece | Role in the agent |
| --- | --- |
| `actions/actions.py`, `actions/snowflake_actions.py` | The tool implementations (`@action` = one tool) |
| `actions/package.yaml` | Tool-layer dependencies for the managed (RCC) environment |
| `scripts/run_local.sh` | Starts the tool layer locally (OpenAPI + MCP on :8080) |
| `.env` | Local credentials for the tool layer (gitignored) |
| `agents/` (this blueprint's Step 3) | The brain: loop, system prompt, tool wrappers |
| `http://localhost:8080/openapi.json` | Tool schemas, HTTP flavor |
| `http://localhost:8080/mcp` | Tool schemas, MCP flavor — for MCP-native hosts |
