# CLAUDE.md

## Session workflow

These behaviors are configured in `.claude/settings.json` and apply to every Claude Code session in this repository:

- **Compact mid-task**: `autoCompactEnabled` is on, so long sessions compact automatically when the context window fills rather than stopping partway through a task. `precomputeCompactionEnabled` prepares the compaction summary in the background so the pause is minimal. Work continues from the summary after compaction — do not wrap up early because a session is getting long.
- **Clear old tasks on session start**: a `SessionStart` hook (matcher `startup`) moves task/todo state left over from previous sessions (`~/.claude/todos` and `~/.claude/tasks`) into `~/.claude/stale-tasks/<timestamp>/`, so each new session begins with a clean task list. The current session's entries are kept, as is anything modified in the last 5 minutes (to avoid clobbering a concurrently running session). The `stale-tasks` folder is only a recovery buffer and is safe to delete at any time.

Because the task list is cleared between sessions, finish or explicitly close out open tasks before ending a session — do not rely on stale tasks carrying over to the next one.
