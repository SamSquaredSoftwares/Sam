"""Snowflake analyst agent — the worked example from docs/AI_AGENT_BLUEPRINT.md.

The Action Server is the tool layer: `actions/snowflake_actions.py` exposes
`query_snowflake` and `test_snowflake_connection` over HTTP, holding the
warehouse credentials on its side. This module is the brain — it wraps those
actions as Claude tools and lets the Anthropic SDK's tool runner drive the loop
until Claude has an answer.

Credentials never meet in one process: this agent needs only
`ANTHROPIC_API_KEY`, while the Snowflake secrets stay with the Action Server
(see the Snowflake configuration section of the README).

Usage:

    ./scripts/run_local.sh                     # start the tool layer first
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m agents.snowflake_analyst "Which 5 customers spent the most?"

Point the agent at a non-default server with --server or SAM_ACTION_SERVER.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
import httpx
from anthropic import beta_tool

DEFAULT_ACTION_PACKAGE = "sam-actions"
DEFAULT_ACTION_SERVER = "http://localhost:8080"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_MAX_ITERATIONS = 20
DEFAULT_TIMEOUT = 120.0

SYSTEM_PROMPT = """You are a data analyst working against a Snowflake warehouse.

Answer the user's question by querying the warehouse with the run_sql tool.
Explore before you assert: when a table is unfamiliar, inspect it (SHOW TABLES,
DESCRIBE TABLE, or a small SELECT) before writing the real query. Prefer a few
small, targeted queries over one large one, and keep result sets small — you
rarely need more than 100 rows to answer a question.

Only read-only statements are accepted; the server rejects anything else, so
don't attempt writes. If a query fails, read the error and adapt rather than
retrying it unchanged. If the failure looks like a configuration or connection
problem rather than bad SQL, call check_connection once to tell the two apart.

Ground every claim in query results from this session. If the data cannot
answer the question, say so plainly instead of guessing. When you are done,
lead with the answer, then give the key numbers that support it."""


class ServerUnavailable(RuntimeError):
    """Raised when the Action Server cannot be reached or serves no actions."""


def action_url(
    name: str,
    *,
    base_url: str = DEFAULT_ACTION_SERVER,
    package: str = DEFAULT_ACTION_PACKAGE,
) -> str:
    """Build the Action Server run endpoint for one action.

    Args:
        name: The action's kebab-case name, e.g. "query-snowflake".
        base_url: Root URL of the Action Server.
        package: The action package segment of the route. The server derives
            this from the directory it serves, so it is worth discovering
            rather than assuming — see `discover_package`.

    Returns:
        The full URL of that action's `/run` endpoint.
    """
    return f"{base_url.rstrip('/')}/api/actions/{package}/{name}/run"


def discover_package(
    base_url: str = DEFAULT_ACTION_SERVER, *, timeout: float = 10.0
) -> str:
    """Discover which action package the server is serving.

    The route segment comes from the directory the Action Server was pointed
    at, not from `package.yaml`: `scripts/run_local.sh` stages into
    `.devrun/sam-actions` and so serves `/api/actions/sam-actions/...`, but a
    server started against another directory serves another segment. Reading
    the OpenAPI spec turns that assumption into a fact, and doubles as the
    reachability check.

    Args:
        base_url: Root URL of the Action Server.
        timeout: Request timeout in seconds.

    Returns:
        The action package segment to use in action URLs.

    Raises:
        ServerUnavailable: If the server is unreachable or exposes no actions.
    """
    spec_url = f"{base_url.rstrip('/')}/openapi.json"
    try:
        response = httpx.get(spec_url, timeout=timeout)
        response.raise_for_status()
        paths = response.json().get("paths", {})
    except (httpx.HTTPError, ValueError) as exc:
        raise ServerUnavailable(
            f"The Action Server is not reachable at {base_url} ({exc}).\n"
            "Start it with ./scripts/run_local.sh, or pass --server / set "
            "SAM_ACTION_SERVER if it runs elsewhere."
        ) from exc

    for path in paths:
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:2] == ["api", "actions"] and parts[-1] == "run":
            return parts[2]

    raise ServerUnavailable(
        f"The server at {base_url} exposes no actions. Check that it was "
        "started against the action package directory."
    )


def run_action(
    name: str,
    payload: dict[str, Any],
    *,
    base_url: str = DEFAULT_ACTION_SERVER,
    package: str = DEFAULT_ACTION_PACKAGE,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Call one action's run endpoint and return its result as text.

    Failures are returned as text rather than raised. A tool result is how the
    agent learns what happened, so a readable error ("relation does not exist")
    lets Claude correct itself, whereas an exception would kill the loop.

    Args:
        name: The action's kebab-case name, e.g. "query-snowflake".
        payload: JSON body for the action's parameters.
        base_url: Root URL of the Action Server.
        package: The action package segment of the route.
        timeout: Per-request timeout in seconds.

    Returns:
        The action's return value as text, or a description of the failure.
    """
    try:
        response = httpx.post(
            action_url(name, base_url=base_url, package=package),
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return (
            f"Could not reach action {name!r} at {base_url}: {exc}. "
            "The Action Server may not be running (./scripts/run_local.sh)."
        )

    if response.status_code != 200:
        return f"Action {name!r} failed (HTTP {response.status_code}): {response.text.strip()}"

    try:
        result = response.json()
    except ValueError:
        return response.text

    # Actions here return strings, which the server sends as a JSON string;
    # anything else is passed through as JSON so Claude still sees the content.
    return result if isinstance(result, str) else json.dumps(result)


def build_tools(
    *,
    base_url: str = DEFAULT_ACTION_SERVER,
    package: str = DEFAULT_ACTION_PACKAGE,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Any]:
    """Build the agent's tools, bound to one Action Server.

    Docstrings here become the tool descriptions Claude reads, so they say when
    to use each tool, not just what it does.

    Args:
        base_url: Root URL of the Action Server backing these tools.
        package: The action package segment of the route.
        timeout: Per-request timeout in seconds.

    Returns:
        Tools ready to pass to the SDK tool runner.
    """

    @beta_tool
    def run_sql(sql: str, max_rows: int = 100) -> str:
        """Run a single read-only SQL statement against Snowflake.

        Use this for every question about warehouse data, including exploring
        what tables and columns exist. Only SELECT / SHOW / DESCRIBE / WITH /
        EXPLAIN statements are accepted; the server rejects anything else and
        caps results at 1000 rows.

        Args:
            sql: The SQL statement to run, without a trailing semicolon.
            max_rows: Maximum rows to return. Keep this small — every row comes
                back into the conversation as JSON.

        Returns:
            JSON with `columns`, `rows`, `row_count`, and `truncated`.
        """
        return run_action(
            "query-snowflake",
            {"sql": sql, "max_rows": max_rows},
            base_url=base_url,
            package=package,
            timeout=timeout,
        )

    @beta_tool
    def check_connection() -> str:
        """Verify Snowflake connectivity and report the session context.

        Use this when a query fails in a way that looks like a configuration or
        connection problem rather than bad SQL, to tell the two apart. It is not
        needed before a first query that works.

        Returns:
            The Snowflake version and current account, user, role, warehouse,
            database, and schema.
        """
        return run_action(
            "test-snowflake-connection",
            {},
            base_url=base_url,
            package=package,
            timeout=timeout,
        )

    return [run_sql, check_connection]


@dataclass
class AgentResult:
    """The outcome of one agent run."""

    answer: str
    stop_reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def refused(self) -> bool:
        """Whether safety classifiers declined the request."""
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        """Whether the answer was cut off by the output token limit."""
        return self.stop_reason == "max_tokens"


def run_agent(
    question: str,
    *,
    client: anthropic.Anthropic | None = None,
    base_url: str = DEFAULT_ACTION_SERVER,
    package: str = DEFAULT_ACTION_PACKAGE,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    timeout: float = DEFAULT_TIMEOUT,
    on_text: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
) -> AgentResult:
    """Answer a question by letting Claude query Snowflake through the actions.

    The tool runner owns the loop: it sends the request, runs whichever tool
    Claude asks for, feeds the result back, and stops when Claude stops calling
    tools. Each iteration yields the assistant message *before* its tools run,
    which is where the callbacks below observe the run.

    Args:
        question: The analytical question to answer.
        client: An Anthropic client. Defaults to one built from the environment.
        base_url: Root URL of the Action Server providing the tools.
        package: The action package segment of the route.
        model: Claude model id.
        effort: Reasoning effort — low, medium, high, xhigh, or max.
        max_tokens: Output token ceiling per turn.
        max_iterations: Hard cap on model turns, so a confused agent cannot loop
            forever.
        timeout: Per-action HTTP timeout in seconds.
        on_text: Called with assistant text as each turn arrives.
        on_tool_call: Called with (tool name, input) as each call is requested.

    Returns:
        The final answer plus what it took to get there.

    Raises:
        anthropic.APIError: If the Messages API call fails.
    """
    client = client or anthropic.Anthropic()

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=max_tokens,
        # Frozen prefix, marked for caching. Note claude-opus-5 only caches
        # prefixes of 512+ tokens, so a prompt this short reports no cache
        # reads — the marker starts paying off as the prompt or tool set grows.
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        # Thinking is on by default on claude-opus-5; effort is the cost lever.
        output_config={"effort": effort},
        tools=build_tools(base_url=base_url, package=package, timeout=timeout),
        max_iterations=max_iterations,
        messages=[{"role": "user", "content": question}],
    )

    result = AgentResult(answer="")
    for message in runner:
        turn_text: list[str] = []
        for block in message.content:
            if block.type == "text":
                if block.text.strip():
                    turn_text.append(block.text)
                    if on_text is not None:
                        on_text(block.text)
            elif block.type == "tool_use":
                result.tool_calls.append(block.name)
                if on_tool_call is not None:
                    on_tool_call(block.name, dict(block.input or {}))

        usage = message.usage
        result.input_tokens += usage.input_tokens or 0
        result.output_tokens += usage.output_tokens or 0
        result.cache_read_tokens += usage.cache_read_input_tokens or 0
        result.stop_reason = message.stop_reason

        # The last turn that produces text is the answer; later turns only run
        # tools, so this keeps the most recent prose rather than concatenating.
        if turn_text:
            result.answer = "\n".join(turn_text)

    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="snowflake_analyst",
        description="Answer questions about Snowflake data using Claude and the Sam actions.",
    )
    parser.add_argument("question", nargs="+", help="The question to answer.")
    parser.add_argument(
        "--server",
        default=os.environ.get("SAM_ACTION_SERVER", DEFAULT_ACTION_SERVER),
        help="Action Server URL (default: %(default)s).",
    )
    parser.add_argument(
        "--package",
        default=None,
        help="Action package route segment (default: read from the server's OpenAPI spec).",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="Claude model id (default: %(default)s)."
    )
    parser.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort (default: %(default)s).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="Maximum model turns (default: %(default)s).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the final answer, without tool-call progress.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the agent from the command line.

    Args:
        argv: Command-line arguments, defaulting to sys.argv[1:].

    Returns:
        A process exit code.
    """
    args = _parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Export it before running the agent.",
            file=sys.stderr,
        )
        return 1

    try:
        package = args.package or discover_package(args.server)
    except ServerUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1

    def report_tool_call(name: str, tool_input: dict[str, Any]) -> None:
        detail = tool_input.get("sql", "")
        suffix = f": {detail}" if detail else ""
        print(f"[{name}{suffix}]", file=sys.stderr, flush=True)

    try:
        result = run_agent(
            " ".join(args.question),
            base_url=args.server,
            package=package,
            model=args.model,
            effort=args.effort,
            max_iterations=args.max_iterations,
            on_tool_call=None if args.quiet else report_tool_call,
        )
    except anthropic.AuthenticationError:
        print("ANTHROPIC_API_KEY was rejected by the API.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("Rate limited by the Anthropic API. Try again shortly.", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError as exc:
        print(f"Could not reach the Anthropic API: {exc}", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"Anthropic API error ({exc.status_code}): {exc.message}", file=sys.stderr)
        return 1

    if result.refused:
        print("The model declined to answer this request.", file=sys.stderr)
        return 1

    print(result.answer)

    if result.truncated:
        print(
            "\n[Answer was cut off by the output limit — ask a narrower question.]",
            file=sys.stderr,
        )
    if not args.quiet:
        print(
            f"\n[{len(result.tool_calls)} tool calls | "
            f"{result.input_tokens} in / {result.output_tokens} out tokens | "
            f"{result.cache_read_tokens} cached]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
