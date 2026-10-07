# Sam Squared — Managed Subagents

Organization-wide Claude Code **managed subagents** for Sam Squared, plus a
cross-platform installer that deploys them into the Claude Code managed-settings
directory.

Managed subagents are deployed by organization administrators. Once installed on
a machine, every Claude Code session on that machine can use them, and they
**take precedence over project (`.claude/agents/`) and user (`~/.claude/agents/`)
subagents with the same name.** This directory is the source of truth; run the
installer to push the agents onto a developer or venue machine.

---

## What's included

| Agent | Category | Model | Purpose |
|-------|----------|-------|---------|
| `code-reviewer` | Core dev quality | opus | Reviews a diff for correctness, security, readability; read-only. |
| `test-writer` | Core dev quality | opus | Writes/extends tests matching the project's framework. |
| `security-reviewer` | Core dev quality | opus | Finds vulnerabilities before merge/release; read-only. |
| `debugger` | Core dev quality | opus | Reproduces, root-causes, and fixes failures. |
| `docs-writer` | Core dev quality | inherit | Keeps READMEs/API docs/docstrings accurate to the code. |
| `anthropic-sdk-expert` | Sam stack | opus | Claude API / `anthropic` Python SDK integration and debugging. |
| `python-pro` | Sam stack | opus | Idiomatic, typed, tested Python in the project's conventions. |
| `samepos-specialist` | Sam stack | opus | SAMePOS node install/commissioning, Digitot migration, POS domain logic. |
| `pr-reviewer` | Release/ops | opus | End-to-end PR review with a prioritized verdict. |
| `release-notes-writer` | Release/ops | inherit | Changelog/release notes from commits & PRs. |
| `dependency-auditor` | Release/ops | opus | Vulnerability, staleness, and license audit of dependencies. |

Definitions live in [`agents/`](agents/). Each is a markdown file with YAML
frontmatter (`name`, `description`, `tools`, `model`, `color`) followed by the
agent's system prompt — the same format as project and user subagents.

Per Sam Squared engineering policy, every code-touching agent is pinned to the
strongest model (`model: opus`); documentation/notes agents use `inherit` so
they follow the session's model.

---

## How managed subagents are located

Claude Code reads managed subagents from `.claude/agents/*.md` inside the
platform managed-settings directory:

| OS | Managed-settings directory | Managed agents directory |
|----|---------------------------|--------------------------|
| **macOS** | `/Library/Application Support/ClaudeCode/` | `…/.claude/agents/` |
| **Linux / WSL** | `/etc/claude-code/` | `…/.claude/agents/` |
| **Windows** | `C:\Program Files\ClaudeCode\` | `…\.claude\agents\` |

> The legacy Windows location `C:\ProgramData\ClaudeCode\` is deprecated
> (removed in Claude Code v2.1.75). Use `C:\Program Files\ClaudeCode\`.

**Precedence (highest to lowest):** managed → session `--agents` → project
`.claude/agents/` → user `~/.claude/agents/` → plugin agents. A managed agent
therefore overrides a project or user agent of the same name.

---

## Install

### macOS / Linux / WSL

```bash
cd managed-agents
sudo ./install.sh              # validates, backs up any existing managed agents, installs
sudo ./install.sh --dry-run    # preview only; makes no changes
sudo ./install.sh --force      # overwrite without creating a backup
```

### Windows (elevated PowerShell)

```powershell
cd managed-agents
.\install.ps1                  # validates and installs
.\install.ps1 -DryRun          # preview only
.\install.ps1 -Force           # overwrite without backup
```

Both installers:

1. Validate every agent definition before touching the system (see below).
2. Back up any pre-existing managed agents to a timestamped
   `…/agents.backup-<timestamp>` directory (unless `--force` / `-Force`).
3. Copy the definitions into the OS managed-agents directory with `0644`
   permissions.

Use `--managed-dir <path>` (bash) or `-ManagedDir <path>` (PowerShell) to target
a non-default location — useful for testing or non-standard installs.

Administrator/root is required because the default target lives under a
system-owned directory.

## Verify

From any project, with the Claude Code CLI:

```bash
claude          # then run /agents, or ask "which subagents are available?"
```

To confirm non-interactively that the managed agents are discovered and loaded:

```bash
# A real managed name resolves and runs:
claude -p "reply: ok" --agent samepos-specialist
# A bogus name prints the full available-agents roster (which now includes ours):
claude -p "x" --agent does-not-exist
```

## Uninstall

Only the agent files shipped in [`agents/`](agents/) are removed; anything else
in the managed agents directory (and any `*.backup-*` folders) is left in place.

```bash
sudo ./uninstall.sh            # remove the shipped managed agents
sudo ./uninstall.sh --dry-run  # preview
```

```powershell
.\uninstall.ps1                # remove the shipped managed agents
.\uninstall.ps1 -DryRun        # preview
```

---

## Adding or editing an agent

1. Add or edit a markdown file in [`agents/`](agents/). The filename stem must
   equal the frontmatter `name` (lowercase letters, digits, hyphens).
2. Provide a required `name` and `description`. The `description` is what Claude
   uses to decide when to delegate — make it specific and action-oriented.
3. Optionally set `tools` (allowlist; omit to inherit all), `model`
   (`opus`/`sonnet`/`haiku`/`fable`/`inherit` or a full `claude-*` id), and
   `color`.
4. Write the system prompt as the markdown body.
5. Validate, then re-run the installer.

> **Portability note:** avoid listing MCP tools (e.g. `mcp__github__…`) in a
> `tools` allowlist unless every target machine has that MCP server, or the
> agent won't be able to use them. Agents that benefit from MCP (e.g.
> `pr-reviewer`) omit `tools` so they inherit the full toolset when available.

## Validation & CI

A validator checks every definition (valid YAML frontmatter, required fields,
`name` matches filename and convention, no duplicate names, allowed
`tools`/`model`/`color` values, non-empty body):

```bash
python3 managed-agents/scripts/validate_agents.py          # validates managed-agents/agents/
python3 -m pytest tests/test_managed_agents.py             # same checks, per-file, for CI
```

The installers run this validator automatically before installing (when
`python3` is available) and abort if any definition is invalid.
