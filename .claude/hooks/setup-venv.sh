#!/usr/bin/env bash
# SessionStart hook: make `.venv/bin/python -m pytest tests/ -q` work from the
# first prompt of a session, so Claude does not have to build the environment
# by hand before it can run anything.
#
# A no-op once .venv exists, so only the first session in a fresh clone pays
# the install. Always exits 0: a failed setup should degrade to "run the
# commands in CLAUDE.md yourself", never block the session from starting.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

# Not this repo (or a partial checkout) — nothing to build.
[ -f requirements.txt ] || exit 0

# Already usable. This is the path almost every session takes.
[ -x .venv/bin/pytest ] && exit 0

echo "Building .venv from requirements.txt (first run in this clone)..." >&2

if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv >&2 &&
        uv pip install --python .venv/bin/python -r requirements.txt >&2
else
    # No uv: fall back to stdlib venv. 3.12 is the floor (see CLAUDE.md).
    python3 -m venv .venv >&2 &&
        .venv/bin/python -m pip install -q -r requirements.txt >&2
fi

if [ -x .venv/bin/pytest ]; then
    echo "Ready: .venv/bin/python -m pytest tests/ -q" >&2
else
    echo "warning: could not build .venv — see the Commands section of CLAUDE.md" >&2
fi

exit 0
