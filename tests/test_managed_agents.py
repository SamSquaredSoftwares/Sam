"""Pytest suite for the managed subagent definitions.

Runs the same validation as ``managed-agents/scripts/validate_agents.py`` but
surfaces a clear per-file assertion so CI failures point at the offending
agent. Run with ``pytest tests/test_managed_agents.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = REPO_ROOT / "managed-agents" / "scripts" / "validate_agents.py"
AGENTS_DIR = REPO_ROOT / "managed-agents" / "agents"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_agents", VALIDATOR_PATH)
    assert spec and spec.loader, f"cannot load validator at {VALIDATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()

# Every category the deployment promises should be represented.
EXPECTED_AGENTS = {
    # Core dev quality set
    "code-reviewer",
    "test-writer",
    "security-reviewer",
    "debugger",
    "docs-writer",
    # Sam-stack specific
    "anthropic-sdk-expert",
    "python-pro",
    "samepos-specialist",
    # Release / ops
    "pr-reviewer",
    "release-notes-writer",
    "dependency-auditor",
}


def test_validator_and_agents_dir_exist():
    assert VALIDATOR_PATH.is_file(), f"validator missing: {VALIDATOR_PATH}"
    assert AGENTS_DIR.is_dir(), f"agents dir missing: {AGENTS_DIR}"


def test_agent_files_present():
    files = validator.collect_files(AGENTS_DIR)
    assert files, "no agent .md files found"


@pytest.mark.parametrize(
    "path",
    validator.collect_files(AGENTS_DIR),
    ids=lambda p: p.name,
)
def test_agent_file_is_valid(path):
    errors = validator.validate_file(path)
    assert not errors, f"{path.name} has validation errors: {errors}"


def test_no_duplicate_names_and_all_valid():
    results = validator.validate_all(AGENTS_DIR)
    failed = {p.name: errs for p, errs in results.items() if errs}
    assert not failed, f"validation failures: {failed}"


def test_expected_agents_are_all_present():
    names = {p.stem for p in validator.collect_files(AGENTS_DIR)}
    missing = EXPECTED_AGENTS - names
    assert not missing, f"expected managed agents missing: {sorted(missing)}"
