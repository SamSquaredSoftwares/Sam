#!/usr/bin/env python3
"""Read a `.env` file into the environment without shell semantics.

The repo documents `.env` as the way to configure credentials, but the two
Snowflake scripts only ever read `os.environ`, so a value that lived only in
`.env` never reached them -- while one of them told the user to put it there.
This module closes that gap.

It deliberately does *not* interpret the file as shell. A Snowflake password
is arbitrary text: `Sn0w$Flake!2026` must survive intact, and a value with a
space in it must not abort anything. So the grammar here is small and literal,
and `scripts/run_local.sh` implements the same grammar in bash:

  * blank lines and lines whose first non-space character is `#` are skipped
  * a leading `export ` is ignored
  * the line splits on its **first** `=`; a line without one is skipped
  * the key is stripped of surrounding space and must look like a shell name
  * the value is taken **verbatim** -- no expansion, no command substitution,
    no trailing-comment stripping (a `#` can appear in a password)
  * one matching pair of surrounding single or double quotes is removed

Values already present in the environment win, so `FOO=bar python script.py`
still overrides the file, as with any other dotenv implementation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

# Same shape bash accepts for an exported name, so the two parsers agree.
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(text: str) -> dict[str, str]:
    """Return the key/value pairs in `text`. Unparseable lines are skipped."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.lstrip().rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(
    path: str | os.PathLike[str] = ".env",
    env: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Load `path` into `env`, without overriding what is already set.

    Returns the names that were applied, so a caller can say what it picked up.
    A missing file is not an error: `.env` is optional everywhere it is used.
    """
    env = os.environ if env is None else env
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SystemExit(f"error: could not read {file}: {e}")

    applied = []
    for key, value in parse(text).items():
        if key not in env:
            env[key] = value
            applied.append(key)
    return applied


def load_repo_dotenv(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Load the repo-root `.env`, wherever the script was invoked from."""
    return load(Path(__file__).resolve().parent.parent / ".env", env)
