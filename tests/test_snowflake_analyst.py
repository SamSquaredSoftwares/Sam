"""Pytest suite for the Snowflake analyst agent.

Covers the parts that run without network access: URL construction, the action
HTTP wrapper (including its failure paths), the generated tool schemas, and the
agent loop driven against a stubbed tool runner. Run with
``pytest tests/test_snowflake_analyst.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("anthropic", reason="anthropic is required to import the agent")
pytest.importorskip("httpx")

import httpx  # noqa: E402  (imported after the skip guard above)

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PATH = REPO_ROOT / "agents" / "snowflake_analyst.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("snowflake_analyst", AGENT_PATH)
    assert spec and spec.loader, f"cannot load agent at {AGENT_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves its own module through
    # sys.modules, and fails on a module loaded straight from a path.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = _load_agent()


# --- URL building ----------------------------------------------------------


def test_action_url_matches_action_server_route():
    url = agent.action_url("query-snowflake", base_url="http://localhost:8080")
    assert url == "http://localhost:8080/api/actions/sam-actions/query-snowflake/run"


def test_action_url_tolerates_trailing_slash():
    assert agent.action_url(
        "health-check", base_url="http://example.com:9000/"
    ) == "http://example.com:9000/api/actions/sam-actions/health-check/run"


def test_action_url_honours_a_custom_package():
    # The server derives this segment from the directory it serves, so it is
    # not always "sam-actions".
    assert agent.action_url("health-check", package="stage").endswith(
        "/api/actions/stage/health-check/run"
    )


# --- run_action ------------------------------------------------------------


class _FakePost:
    """Records the last request and returns a canned response."""

    def __init__(self, response: httpx.Response | Exception):
        self.response = response
        self.url: str | None = None
        self.json_body: dict[str, Any] | None = None

    def __call__(self, url: str, *, json: dict[str, Any], timeout: float):
        self.url = url
        self.json_body = json
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _response(status: int, payload: Any, *, as_text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://localhost:8080/x")
    if as_text is not None:
        return httpx.Response(status, text=as_text, request=request)
    return httpx.Response(status, json=payload, request=request)


def test_run_action_returns_string_result_unwrapped(monkeypatch):
    body = json.dumps({"columns": ["N"], "rows": [[1]], "row_count": 1})
    post = _FakePost(_response(200, body))
    monkeypatch.setattr(agent.httpx, "post", post)

    result = agent.run_action("query-snowflake", {"sql": "select 1", "max_rows": 5})

    # Actions return strings; the agent must hand Claude the string itself,
    # not a re-encoded JSON string wrapping it.
    assert result == body
    assert post.url.endswith("/sam-actions/query-snowflake/run")
    assert post.json_body == {"sql": "select 1", "max_rows": 5}


def test_run_action_serialises_non_string_payloads(monkeypatch):
    monkeypatch.setattr(agent.httpx, "post", _FakePost(_response(200, {"ok": True})))
    assert json.loads(agent.run_action("x", {})) == {"ok": True}


def test_run_action_falls_back_to_raw_text_when_not_json(monkeypatch):
    monkeypatch.setattr(
        agent.httpx, "post", _FakePost(_response(200, None, as_text="plain text"))
    )
    assert agent.run_action("x", {}) == "plain text"


def test_run_action_reports_http_errors_as_text(monkeypatch):
    monkeypatch.setattr(
        agent.httpx,
        "post",
        _FakePost(_response(500, None, as_text="Only read-only statements are allowed")),
    )
    result = agent.run_action("query-snowflake", {"sql": "drop table t"})

    # Returned, not raised: the agent needs to read this and adapt.
    assert "failed (HTTP 500)" in result
    assert "read-only" in result


def test_run_action_reports_connection_errors_with_a_hint(monkeypatch):
    monkeypatch.setattr(
        agent.httpx, "post", _FakePost(httpx.ConnectError("connection refused"))
    )
    result = agent.run_action("health-check", {})

    assert result.startswith("Could not reach action")
    assert "run_local.sh" in result


# --- tools -----------------------------------------------------------------


def test_tools_expose_expected_schemas():
    tools = {t.to_dict()["name"]: t.to_dict() for t in agent.build_tools()}

    assert set(tools) == {"run_sql", "check_connection"}

    run_sql = tools["run_sql"]
    assert set(run_sql["input_schema"]["properties"]) == {"sql", "max_rows"}
    assert run_sql["input_schema"]["required"] == ["sql"]
    # The description tells Claude when to use it, not only what it does.
    assert "Use this" in run_sql["description"]

    assert tools["check_connection"]["input_schema"]["properties"] == {}


def test_run_sql_tool_posts_to_the_query_action(monkeypatch):
    post = _FakePost(_response(200, "{}"))
    monkeypatch.setattr(agent.httpx, "post", post)

    run_sql = next(t for t in agent.build_tools() if t.to_dict()["name"] == "run_sql")
    run_sql.call({"sql": "select 1", "max_rows": 7})

    assert post.url.endswith("/query-snowflake/run")
    assert post.json_body == {"sql": "select 1", "max_rows": 7}


def test_tools_are_bound_to_the_given_server(monkeypatch):
    post = _FakePost(_response(200, "{}"))
    monkeypatch.setattr(agent.httpx, "post", post)

    tools = agent.build_tools(base_url="http://elsewhere:9001")
    next(t for t in tools if t.to_dict()["name"] == "check_connection").call({})

    assert post.url.startswith("http://elsewhere:9001/")


# --- agent loop ------------------------------------------------------------


class _Block:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _Message:
    def __init__(self, content, stop_reason, usage):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _StubClient:
    """Stands in for anthropic.Anthropic, replaying canned turns."""

    def __init__(self, turns):
        self.turns = turns
        self.kwargs: dict[str, Any] = {}
        outer = self

        class _Messages:
            def tool_runner(self, **kwargs):
                outer.kwargs = kwargs
                return iter(outer.turns)

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def _two_turn_run():
    return [
        _Message(
            [
                _Block(type="text", text="Checking the orders table."),
                _Block(type="tool_use", name="run_sql", input={"sql": "select 1"}),
            ],
            "tool_use",
            _Usage(100, 20, 0),
        ),
        _Message(
            [_Block(type="text", text="Revenue was $42.")],
            "end_turn",
            _Usage(150, 30, 90),
        ),
    ]


def test_run_agent_returns_final_answer_and_accumulates_usage():
    result = agent.run_agent("How much revenue?", client=_StubClient(_two_turn_run()))

    # The last turn's text is the answer, not every turn concatenated.
    assert result.answer == "Revenue was $42."
    assert result.tool_calls == ["run_sql"]
    assert result.stop_reason == "end_turn"
    assert (result.input_tokens, result.output_tokens) == (250, 50)
    assert result.cache_read_tokens == 90
    assert not result.refused and not result.truncated


def test_run_agent_invokes_callbacks():
    seen_text: list[str] = []
    seen_calls: list[tuple[str, dict]] = []

    agent.run_agent(
        "How much revenue?",
        client=_StubClient(_two_turn_run()),
        on_text=seen_text.append,
        on_tool_call=lambda name, params: seen_calls.append((name, params)),
    )

    assert seen_text == ["Checking the orders table.", "Revenue was $42."]
    assert seen_calls == [("run_sql", {"sql": "select 1"})]


def test_run_agent_passes_through_configuration():
    client = _StubClient(_two_turn_run())
    agent.run_agent(
        "q", client=client, model="claude-haiku-4-5", effort="low", max_iterations=3
    )

    assert client.kwargs["model"] == "claude-haiku-4-5"
    assert client.kwargs["output_config"] == {"effort": "low"}
    assert client.kwargs["max_iterations"] == 3
    # Thinking is on by default on these models; the agent must not disable it.
    assert "thinking" not in client.kwargs
    # No sampling parameters — they are rejected on claude-opus-5.
    assert not {"temperature", "top_p", "top_k"} & set(client.kwargs)


def test_run_agent_marks_system_prompt_for_caching():
    client = _StubClient(_two_turn_run())
    agent.run_agent("q", client=client)

    system = client.kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_run_agent_flags_refusal_and_truncation():
    refused = _StubClient([_Message([], "refusal", _Usage())])
    assert agent.run_agent("q", client=refused).refused

    cut_off = _StubClient(
        [_Message([_Block(type="text", text="partial")], "max_tokens", _Usage())]
    )
    assert agent.run_agent("q", client=cut_off).truncated


# --- package discovery -----------------------------------------------------


class _FakeGet:
    def __init__(self, response: httpx.Response | Exception):
        self.response = response
        self.url: str | None = None

    def __call__(self, url: str, *, timeout: float):
        self.url = url
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _spec(*action_names: str, package: str = "sam-actions") -> dict[str, Any]:
    paths = {f"/api/actions/{package}/{name}/run": {} for name in action_names}
    # Real specs also carry non-action routes, which must be ignored.
    paths["/api/secrets"] = {}
    return {"paths": paths}


def test_discover_package_reads_the_openapi_spec(monkeypatch):
    get = _FakeGet(_response(200, _spec("health-check", "query-snowflake")))
    monkeypatch.setattr(agent.httpx, "get", get)

    assert agent.discover_package("http://localhost:8080") == "sam-actions"
    assert get.url == "http://localhost:8080/openapi.json"


def test_discover_package_detects_a_non_default_segment(monkeypatch):
    # An Action Server started against another directory serves another
    # segment; the agent must follow the server rather than assume.
    monkeypatch.setattr(
        agent.httpx, "get", _FakeGet(_response(200, _spec("health-check", package="stage")))
    )
    assert agent.discover_package("http://localhost:8080") == "stage"


def test_discover_package_raises_with_a_hint_when_unreachable(monkeypatch):
    monkeypatch.setattr(agent.httpx, "get", _FakeGet(httpx.ConnectError("refused")))

    with pytest.raises(agent.ServerUnavailable, match="run_local.sh"):
        agent.discover_package("http://localhost:8080")


def test_discover_package_raises_when_no_actions_are_served(monkeypatch):
    monkeypatch.setattr(agent.httpx, "get", _FakeGet(_response(200, {"paths": {}})))

    with pytest.raises(agent.ServerUnavailable, match="no actions"):
        agent.discover_package("http://localhost:8080")


# --- cli -------------------------------------------------------------------


def test_main_requires_an_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert agent.main(["anything"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_reports_a_dead_server_before_calling_the_api(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(agent.httpx, "get", _FakeGet(httpx.ConnectError("refused")))

    def fail(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("the API must not be called when the server is down")

    monkeypatch.setattr(agent, "run_agent", fail)

    assert agent.main(["--server", "http://localhost:9999", "question"]) == 1
    assert "not reachable" in capsys.readouterr().err


def test_main_prints_the_answer(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(agent, "discover_package", lambda *a, **k: "sam-actions")
    monkeypatch.setattr(
        agent,
        "run_agent",
        lambda *a, **k: agent.AgentResult(answer="Revenue was $42.", stop_reason="end_turn"),
    )

    assert agent.main(["--quiet", "How much revenue?"]) == 0
    assert capsys.readouterr().out.strip() == "Revenue was $42."


def test_main_passes_the_discovered_package_to_the_agent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(agent, "discover_package", lambda *a, **k: "discovered-pkg")
    seen: dict[str, Any] = {}

    def capture(question, **kwargs):
        seen.update(kwargs)
        return agent.AgentResult(answer="ok")

    monkeypatch.setattr(agent, "run_agent", capture)
    assert agent.main(["--quiet", "q"]) == 0
    assert seen["package"] == "discovered-pkg"


def test_main_explicit_package_skips_discovery(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def fail(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("discovery must be skipped when --package is given")

    monkeypatch.setattr(agent, "discover_package", fail)
    seen: dict[str, Any] = {}

    def capture(question, **kwargs):
        seen.update(kwargs)
        return agent.AgentResult(answer="ok")

    monkeypatch.setattr(agent, "run_agent", capture)
    assert agent.main(["--quiet", "--package", "manual-pkg", "q"]) == 0
    assert seen["package"] == "manual-pkg"
