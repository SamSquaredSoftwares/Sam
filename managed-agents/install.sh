#!/usr/bin/env bash
#
# install.sh — deploy Sam Squared managed subagents into the Claude Code
# managed-settings directory on macOS or Linux/WSL.
#
# Managed subagents are picked up by every Claude Code session on this machine
# and take precedence over project (.claude/agents/) and user (~/.claude/agents/)
# subagents with the same name. See ./README.md for details.
#
# Usage:
#   sudo ./install.sh [options]
#
# Options:
#   --dry-run            Show what would happen; make no changes.
#   --force              Overwrite existing managed agents without backing up.
#   --managed-dir DIR    Override the managed-settings directory (advanced/testing).
#   -h, --help           Show this help.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_SRC="$SCRIPT_DIR/agents"
VALIDATOR="$SCRIPT_DIR/scripts/validate_agents.py"

DRY_RUN=0
FORCE=0
MANAGED_DIR=""

log()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --managed-dir) shift; MANAGED_DIR="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

# --- Resolve the managed-settings directory for this OS ----------------------
if [ -z "$MANAGED_DIR" ]; then
  case "$(uname -s)" in
    Darwin) MANAGED_DIR="/Library/Application Support/ClaudeCode" ;;
    Linux)  MANAGED_DIR="/etc/claude-code" ;;
    *)
      err "unsupported OS '$(uname -s)'. On Windows run install.ps1 instead."
      exit 1
      ;;
  esac
fi
DEST="$MANAGED_DIR/.claude/agents"

# --- Verify the source -------------------------------------------------------
if [ ! -d "$AGENTS_SRC" ]; then
  err "agents source directory not found: $AGENTS_SRC"
  exit 1
fi

shopt -s nullglob
AGENT_FILES=("$AGENTS_SRC"/*.md)
shopt -u nullglob
if [ ${#AGENT_FILES[@]} -eq 0 ]; then
  err "no agent .md files found in $AGENTS_SRC"
  exit 1
fi

log "Sam Squared — managed subagents installer"
log "  source : $AGENTS_SRC (${#AGENT_FILES[@]} agents)"
log "  target : $DEST"
[ "$DRY_RUN" -eq 1 ] && log "  mode   : DRY RUN (no changes will be made)"
log ""

# --- Pre-flight validation ---------------------------------------------------
if command -v python3 >/dev/null 2>&1 && [ -f "$VALIDATOR" ]; then
  log "Validating agent definitions..."
  if ! python3 "$VALIDATOR" "$AGENTS_SRC"; then
    err "validation failed; aborting install."
    exit 1
  fi
  log ""
else
  info "python3/validator unavailable — skipping deep validation."
fi

# --- Permission check --------------------------------------------------------
# We need to be able to create the managed directory tree. Find the nearest
# existing ancestor and check that it is writable.
check_dir="$DEST"
while [ ! -e "$check_dir" ] && [ "$check_dir" != "/" ]; do
  check_dir="$(dirname "$check_dir")"
done
if [ "$DRY_RUN" -eq 0 ] && [ ! -w "$check_dir" ]; then
  err "no write permission for '$check_dir'."
  err "Re-run with elevated privileges, e.g.:  sudo $0"
  exit 1
fi

# --- Backup existing managed agents ------------------------------------------
if [ -d "$DEST" ]; then
  shopt -s nullglob
  existing=("$DEST"/*.md)
  shopt -u nullglob
  if [ ${#existing[@]} -gt 0 ] && [ "$FORCE" -eq 0 ]; then
    backup="${DEST}.backup-$(date +%Y%m%d-%H%M%S)"
    log "Backing up ${#existing[@]} existing managed agent(s) -> $backup"
    if [ "$DRY_RUN" -eq 0 ]; then
      mkdir -p "$backup"
      cp -p "$DEST"/*.md "$backup"/
    fi
  fi
fi

# --- Install -----------------------------------------------------------------
log "Installing ${#AGENT_FILES[@]} managed subagent(s):"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$DEST"
fi
for f in "${AGENT_FILES[@]}"; do
  info "$(basename "$f")"
  if [ "$DRY_RUN" -eq 0 ]; then
    install -m 0644 "$f" "$DEST/$(basename "$f")"
  fi
done

log ""
if [ "$DRY_RUN" -eq 1 ]; then
  log "Dry run complete. No changes were made."
else
  log "Done. ${#AGENT_FILES[@]} managed subagent(s) installed to:"
  log "  $DEST"
  log ""
  log "Verify from any project with the Claude Code CLI:"
  log "  claude   # then run /agents, or ask 'which subagents are available?'"
  log ""
  log "These managed agents override project and user agents of the same name."
fi
