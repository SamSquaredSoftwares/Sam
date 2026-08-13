#!/usr/bin/env bash
# Run the Sema4.ai Action Server locally without RCC-managed environments.
#
# The Action Server normally bootstraps an isolated environment per action
# package via RCC (downloaded from cdn.sema4.ai). In sandboxed/offline
# environments that host is unreachable, so this script serves the actions in
# "unmanaged" mode instead: the actions run inside the same virtualenv as the
# server, using the dependencies from requirements.txt.
#
# Usage:
#   ./scripts/run_local.sh [port]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8080}"
VENV="$ROOT/.venv"
STAGE="$ROOT/.devrun/sam-actions"

# 1. Ensure the virtualenv exists and has the dependencies.
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv at $VENV ..."
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 "$VENV"
    else
        python3.12 -m venv "$VENV"
    fi
fi

if [ ! -x "$VENV/bin/action-server" ]; then
    echo "Installing dependencies from requirements.txt ..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV/bin/python" -r "$ROOT/requirements.txt"
    else
        "$VENV/bin/pip" install -r "$ROOT/requirements.txt"
    fi
fi

# 2. Stage the actions without package.yaml so the server skips the RCC
#    bootstrap and runs the actions in this venv (unmanaged mode).
mkdir -p "$STAGE"
cp "$ROOT/actions/actions.py" "$STAGE/actions.py"

# 3. Start the server.
echo "Starting Sema4.ai Action Server on http://localhost:$PORT"
exec "$VENV/bin/action-server" start \
    --dir "$STAGE" \
    --datadir "$ROOT/.datadir" \
    --port "$PORT"
