---
name: agent-board
description: Use when multiple coding agents share a Git repository or linked worktrees and need a lightweight local board to announce tasks, claim scopes or files, detect overlapping work, post progress, blockers, messages, or handoffs, and inspect or repair coordination state. Also use when installing this board into another project.
---

# Agent Board

Coordinate coding agents through a deterministic repository-local CLI. The board uses Python standard-library code and local JSON/JSONL files; it does not start agents, run a daemon, or replace Git.

## Install

Resolve `<skill-dir>` to the directory containing this `SKILL.md`, then install from the target Git repository root:

```bash
python <skill-dir>/install.py <project-root>
```

Use shared storage for linked worktrees from the same clone:

```bash
python <skill-dir>/install.py <project-root> --storage git-common
```

The installer preserves existing `AGENTS.md` and `.gitignore` content through managed blocks. Re-run it to upgrade. Use `--no-agents` only when the project maintains coordination rules elsewhere.

## Coordinate Work

Before editing files:

```bash
python scripts/agent_board.py status --active
python scripts/agent_board.py start --agent <stable-id> --tool <agent-tool> \
  --task "<one-line task>" --scope <paths-or-domains> --files <known-files>
```

During work:

```bash
python scripts/agent_board.py update --agent <stable-id> --note "<progress>"
python scripts/agent_board.py conflicts
python scripts/agent_board.py message --agent <stable-id> --to all "<message>"
python scripts/agent_board.py block --agent <stable-id> --blocker "<reason>" --handoff "<context>"
```

When leaving the task:

```bash
python scripts/agent_board.py done --agent <stable-id> --note "<verification>"
# Or, when unfinished:
python scripts/agent_board.py release --agent <stable-id> --handoff "<remaining work>"
```

Use one stable agent ID for one open task. Close or release it before starting another. Treat scope and file conflicts as advisory warnings: inspect the other task and coordinate before editing overlapping files.

## Diagnose

```bash
python scripts/agent_board.py doctor
python scripts/agent_board.py status --active --json
python scripts/agent_board.py events --limit 20 --json
python scripts/agent_board.py repair --from-backup
```

Run `repair` only after `doctor` reports damaged state. Runtime files are ignored by Git; do not commit `state.json`, message/event logs, backups, corrupt snapshots, or lock files.
