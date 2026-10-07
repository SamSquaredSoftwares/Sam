#!/usr/bin/env bash
#
# uninstall.sh — remove Sam Squared managed subagents from the Claude Code
# managed-settings directory on macOS or Linux/WSL.
#
# Only the agent files shipped in ./agents are removed; any other files in the
# managed agents directory are left untouched. Backups created by install.sh
# (*.backup-*) are left in place.
#
# Usage:
#   sudo ./uninstall.sh [options]
#
# Options:
#   --dry-run            Show what would be removed; make no changes.
#   --managed-dir DIR    Override the managed-settings directory (advanced/testing).
#   -h, --help           Show this help.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_SRC="$SCRIPT_DIR/agents"

DRY_RUN=0
MANAGED_DIR=""

log()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --managed-dir) shift; MANAGED_DIR="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

if [ -z "$MANAGED_DIR" ]; then
  case "$(uname -s)" in
    Darwin) MANAGED_DIR="/Library/Application Support/ClaudeCode" ;;
    Linux)  MANAGED_DIR="/etc/claude-code" ;;
    *) err "unsupported OS '$(uname -s)'. On Windows run uninstall.ps1 instead."; exit 1 ;;
  esac
fi
DEST="$MANAGED_DIR/.claude/agents"

if [ ! -d "$DEST" ]; then
  log "Nothing to do: managed agents directory does not exist ($DEST)."
  exit 0
fi

shopt -s nullglob
SHIPPED=("$AGENTS_SRC"/*.md)
shopt -u nullglob
if [ ${#SHIPPED[@]} -eq 0 ]; then
  err "no shipped agent files found in $AGENTS_SRC; cannot determine what to remove."
  exit 1
fi

log "Removing Sam Squared managed subagents from:"
log "  $DEST"
[ "$DRY_RUN" -eq 1 ] && log "  mode: DRY RUN (no changes will be made)"
log ""

removed=0
for f in "${SHIPPED[@]}"; do
  name="$(basename "$f")"
  target="$DEST/$name"
  if [ -f "$target" ]; then
    info "remove $name"
    [ "$DRY_RUN" -eq 0 ] && rm -f "$target"
    removed=$((removed + 1))
  else
    info "skip   $name (not installed)"
  fi
done

# Remove the agents directory if we emptied it (ignore failure if not empty).
if [ "$DRY_RUN" -eq 0 ]; then
  rmdir "$DEST" 2>/dev/null || true
fi

log ""
if [ "$DRY_RUN" -eq 1 ]; then
  log "Dry run complete. $removed agent(s) would be removed."
else
  log "Done. Removed $removed managed subagent(s)."
fi
