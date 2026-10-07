"""Tests for reading `.env` without shell semantics.

Two things are pinned here. First, that a `.env` value survives intact: a
Snowflake password is arbitrary text, and `run_local.sh` used to `.` the file,
which ran it as shell -- `Sn0w$Flake!2026` arrived as `Sn0w!2026` and a value
with a space aborted the server under `set -e`.

Second, that the bash reader in `scripts/load_dotenv.sh` and the Python one in
`scripts/dotenv_file.py` agree. They exist separately because `run_local.sh`
loads `.env` before the virtualenv is built, so the parity is asserted here
rather than assumed.
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "dotenv_file.py"
BASH_LOADER = ROOT / "scripts" / "load_dotenv.sh"

_spec = importlib.util.spec_from_file_location("dotenv_file", MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["dotenv_file"] = _module
_spec.loader.exec_module(_module)

parse = _module.parse
load = _module.load


# The values that motivated the change, plus the syntax the file supports.
FIXTURE = """\
# Snowflake, as .env.example lays it out
SNOWFLAKE_ACCOUNT=myorg-myaccount
SNOWFLAKE_USER=SAM_SERVICE

# The shell-sourcing casualties.
DOLLAR_PASSWORD=Sn0w$Flake!2026
BACKTICK_PASSWORD=a`whoami`b
SPACED="two words"
SEMICOLONS=a;b;c
HASH_IN_VALUE=pa#ssword
QUOTED_SINGLE='literal $HOME'
GLOB=*
PARENS=(not a subshell)

export EXPORTED=yes
  INDENTED=also-yes
EMPTY=
EQUALS_IN_VALUE=a=b=c

no_equals_sign_here
1_BAD_KEY=skipped
BAD-KEY=skipped
"""

EXPECTED = {
    "SNOWFLAKE_ACCOUNT": "myorg-myaccount",
    "SNOWFLAKE_USER": "SAM_SERVICE",
    "DOLLAR_PASSWORD": "Sn0w$Flake!2026",
    "BACKTICK_PASSWORD": "a`whoami`b",
    "SPACED": "two words",
    "SEMICOLONS": "a;b;c",
    "HASH_IN_VALUE": "pa#ssword",
    "QUOTED_SINGLE": "literal $HOME",
    "GLOB": "*",
    "PARENS": "(not a subshell)",
    "EXPORTED": "yes",
    "INDENTED": "also-yes",
    "EMPTY": "",
    "EQUALS_IN_VALUE": "a=b=c",
}


def test_python_parser_takes_values_literally():
    assert parse(FIXTURE) == EXPECTED


@pytest.mark.parametrize("key", ["no_equals_sign_here", "1_BAD_KEY", "BAD-KEY"])
def test_unusable_lines_are_skipped(key):
    assert key not in parse(FIXTURE)


def test_crlf_line_endings_do_not_leak_into_values():
    assert parse("SNOWFLAKE_PASSWORD=secret\r\n") == {"SNOWFLAKE_PASSWORD": "secret"}


def test_existing_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SNOWFLAKE_ROLE=FROM_FILE\nSNOWFLAKE_USER=FROM_FILE\n")
    env = {"SNOWFLAKE_ROLE": "FROM_ENV"}
    applied = load(env_file, env)
    assert env["SNOWFLAKE_ROLE"] == "FROM_ENV"
    assert env["SNOWFLAKE_USER"] == "FROM_FILE"
    assert applied == ["SNOWFLAKE_USER"]


def test_a_missing_file_is_not_an_error(tmp_path):
    env = {}
    assert load(tmp_path / "nope.env", env) == []
    assert env == {}


# --------------------------------------------------------------------------
# Parity with the bash reader run_local.sh uses.
# --------------------------------------------------------------------------
READ_BACK = (
    "import json, os, sys; "
    "print(json.dumps({k: os.environ[k] for k in sys.argv[1:] if k in os.environ}))"
)


def bash_parse(env_file: Path) -> dict[str, str]:
    """Run scripts/load_dotenv.sh over a file and report what it exported."""
    script = f"""
        set -euo pipefail
        . {shlex.quote(str(BASH_LOADER))}
        load_dotenv {shlex.quote(str(env_file))}
        {shlex.quote(sys.executable)} -c {shlex.quote(READ_BACK)} {" ".join(EXPECTED)}
    """
    # A controlled environment, because load_dotenv leaves already-set names
    # alone -- an ambient variable sharing a fixture's name would look like a
    # parser disagreement.
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 0, f"bash loader failed:\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_bash_and_python_readers_agree(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(FIXTURE)
    assert bash_parse(env_file) == EXPECTED


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_bash_reader_does_not_execute_the_value(tmp_path):
    # `. .env` would have run this and exported the command's output.
    env_file = tmp_path / ".env"
    env_file.write_text("SNOWFLAKE_PASSWORD=a`id -u`b\n")
    script = f"""
        set -euo pipefail
        . {BASH_LOADER!s}
        load_dotenv {env_file!s}
        printf '%s' "$SNOWFLAKE_PASSWORD"
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout == "a`id -u`b"
