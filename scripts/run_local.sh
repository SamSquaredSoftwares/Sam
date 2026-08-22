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

# Virtualenv layout differs by platform: POSIX puts executables in bin/,
# Windows (native CPython or uv) in Scripts/. Resolve after the venv exists.
venv_bin() {
    if [ -d "$VENV/Scripts" ]; then echo "$VENV/Scripts"; else echo "$VENV/bin"; fi
}

# 0. Load credentials from .env if present (see .env.example).
if [ -f "$ROOT/.env" ]; then
    echo "Loading environment from $ROOT/.env"
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi

# 1. Ensure the virtualenv exists and has the dependencies.
if [ ! -x "$(venv_bin)/python" ] && [ ! -x "$(venv_bin)/python.exe" ]; then
    echo "Creating virtualenv at $VENV ..."
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 "$VENV"
    else
        python3.12 -m venv "$VENV"
    fi
fi
BIN="$(venv_bin)"
PY="$BIN/python.exe"; [ -f "$PY" ] || PY="$BIN/python"

if [ ! -x "$BIN/action-server" ] && [ ! -x "$BIN/action-server.exe" ]; then
    echo "Installing dependencies from requirements.txt ..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$PY" -r "$ROOT/requirements.txt"
    else
        "$BIN/pip" install -r "$ROOT/requirements.txt"
    fi
fi

# 2. Stage the actions without package.yaml so the server skips the RCC
#    bootstrap and runs the actions in this venv (unmanaged mode).
mkdir -p "$STAGE"
cp "$ROOT/actions/"*.py "$STAGE/"

# 3. Start the server.
AS="$BIN/action-server.exe"; [ -f "$AS" ] || AS="$BIN/action-server"
echo "Starting Sema4.ai Action Server on http://localhost:$PORT"
exec "$AS" start \
    --dir "$STAGE" \
    --datadir "$ROOT/.datadir" \
    --port "$PORT"
