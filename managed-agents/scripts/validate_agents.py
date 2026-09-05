#!/usr/bin/env python3
"""Validate Sam Squared managed subagent definitions.

Checks every ``*.md`` file in the ``agents/`` directory for a well-formed
subagent definition: valid YAML frontmatter, required fields, a name that
matches the filename and the ``[a-z][a-z0-9-]*`` convention, no duplicate
names, allowed ``tools`` / ``model`` / ``color`` values, and a non-empty
system-prompt body.

Exit code is 0 when every file is valid, 1 otherwise. Intended to be run
directly (``python3 validate_agents.py``) and from the pytest suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only without PyYAML
    yaml = None  # type: ignore[assignment]

# Directory holding the agent markdown files (sibling of this script's parent).
AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# Core Claude Code tools that may appear in a subagent `tools` allowlist.
# MCP tools (``mcp__server`` / ``mcp__server__tool``) and restricted spawning
# (``Agent(agent_type)``) are also accepted via the patterns below.
CORE_TOOLS = {
    "Bash", "Edit", "Read", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite", "Task", "Agent",
}
MCP_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_.-]+(__[A-Za-z0-9_.*-]+)?$")
AGENT_TOOL_RE = re.compile(r"^Agent\([A-Za-z0-9_-]+\)$")

MODEL_ALIASES = {"inherit", "opus", "sonnet", "haiku", "fable"}
MODEL_ID_RE = re.compile(r"^claude-[A-Za-z0-9._-]+$")

ALLOWED_COLORS = {
    "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan",
}

REQUIRED_FIELDS = ("name", "description")


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return (frontmatter_yaml, body) or None if no frontmatter block."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _normalise_tools(value: Any) -> list[str] | None:
    """Accept a comma-separated string or a YAML list; return a list or None."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value]
    return None


def validate_file(path: Path) -> list[str]:
    """Validate a single agent file; return a list of human-readable errors."""
    errors: list[str] = []
    raw = path.read_text(encoding="utf-8")

    split = _split_frontmatter(raw)
    if split is None:
        errors.append("missing YAML frontmatter (file must start with a '---' block)")
        return errors
    fm_text, body = split

    if yaml is None:
        errors.append("PyYAML is not installed; cannot parse frontmatter")
        return errors

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:  # pragma: no cover - malformed YAML is rare
        errors.append(f"frontmatter is not valid YAML: {exc}")
        return errors

    if not isinstance(meta, dict):
        errors.append("frontmatter must be a YAML mapping of key: value pairs")
        return errors

    # Required fields.
    for field in REQUIRED_FIELDS:
        if field not in meta or meta[field] in (None, ""):
            errors.append(f"missing required field '{field}'")

    # name: format + filename match.
    name = meta.get("name")
    if isinstance(name, str):
        if not NAME_RE.match(name):
            errors.append(
                f"name '{name}' must be lowercase letters, digits and hyphens "
                "and start with a letter"
            )
        if name != path.stem:
            errors.append(
                f"name '{name}' does not match filename stem '{path.stem}'"
            )
    elif name is not None:
        errors.append("name must be a string")

    # description: non-empty string.
    description = meta.get("description")
    if description is not None and (
        not isinstance(description, str) or not description.strip()
    ):
        errors.append("description must be a non-empty string")

    # tools: known core tools, MCP tools, or Agent(...) restricted spawning.
    if "tools" in meta:
        tools = _normalise_tools(meta["tools"])
        if tools is None:
            errors.append("tools must be a comma-separated string or a list")
        else:
            for tool in tools:
                if (
                    tool not in CORE_TOOLS
                    and not MCP_TOOL_RE.match(tool)
                    and not AGENT_TOOL_RE.match(tool)
                ):
                    errors.append(f"unknown tool in tools allowlist: '{tool}'")

    # model: alias or full claude- model id.
    if "model" in meta:
        model = meta["model"]
        if not isinstance(model, str) or (
            model not in MODEL_ALIASES and not MODEL_ID_RE.match(model)
        ):
            errors.append(
                f"model '{model}' must be one of {sorted(MODEL_ALIASES)} "
                "or a full 'claude-*' model id"
            )

    # color: allowed set.
    if "color" in meta:
        color = meta["color"]
        if color not in ALLOWED_COLORS:
            errors.append(
                f"color '{color}' must be one of {sorted(ALLOWED_COLORS)}"
            )

    # body: non-empty system prompt.
    if not body.strip():
        errors.append("body (system prompt) is empty")

    return errors


def collect_files(agents_dir: Path = AGENTS_DIR) -> list[Path]:
    return sorted(agents_dir.glob("*.md"))


def validate_all(agents_dir: Path = AGENTS_DIR) -> dict[Path, list[str]]:
    """Validate every agent file plus cross-file checks (duplicate names)."""
    files = collect_files(agents_dir)
    results: dict[Path, list[str]] = {path: validate_file(path) for path in files}

    # Cross-file: duplicate names.
    seen: dict[str, Path] = {}
    if yaml is not None:
        for path in files:
            split = _split_frontmatter(path.read_text(encoding="utf-8"))
            if split is None:
                continue
            try:
                meta = yaml.safe_load(split[0]) or {}
            except yaml.YAMLError:
                continue
            name = meta.get("name") if isinstance(meta, dict) else None
            if not isinstance(name, str):
                continue
            if name in seen:
                results[path].append(
                    f"duplicate agent name '{name}' (also in {seen[name].name})"
                )
            else:
                seen[name] = path

    return results


def main() -> int:
    agents_dir = AGENTS_DIR
    if len(sys.argv) > 1:
        agents_dir = Path(sys.argv[1]).resolve()

    if not agents_dir.is_dir():
        print(f"ERROR: agents directory not found: {agents_dir}", file=sys.stderr)
        return 1

    results = validate_all(agents_dir)
    if not results:
        print(f"ERROR: no agent .md files found in {agents_dir}", file=sys.stderr)
        return 1

    total = len(results)
    failed = {path: errs for path, errs in results.items() if errs}

    for path in sorted(results):
        errs = results[path]
        if errs:
            print(f"FAIL {path.name}")
            for err in errs:
                print(f"     - {err}")
        else:
            print(f"OK   {path.name}")

    print()
    if failed:
        print(f"{len(failed)}/{total} agent file(s) FAILED validation.")
        return 1
    print(f"All {total} managed subagent definitions are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
