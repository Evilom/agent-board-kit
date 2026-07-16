# Agent Board Kit

English | [简体中文](README.zh-CN.md)

Agent Board Kit is a lightweight local coordination board for coding agents such as Codex, Claude Code, Cursor, and ZCode. It records task ownership, claimed scopes and files, progress, blockers, messages, and handoffs through JSON/JSONL files protected by cross-process locks.

It requires only Python 3.8+. There is no daemon, database, Node.js runtime, or third-party Python dependency.

Version `1.1.1` provides task lifecycle validation, scope and file conflict warnings, state backup and repair, structured JSON output, and optional shared storage for linked Git worktrees.

## When To Use It

- Several coding agents work in the same Git checkout and need to announce their work before editing files.
- Several linked worktrees from one clone need to share task state.
- You want deterministic, locally auditable coordination without deploying a server, database, Web UI, or full agent orchestration platform.

Agent Board Kit coordinates work; it does not create agents, schedule models, or execute tasks. It can be combined with a larger orchestration or messaging system when cross-machine coordination is required.

## Install As An Agent Skill

Install it with the open-source [Skills CLI](https://github.com/vercel-labs/skills):

```powershell
npx skills add Evilom/agent-board-kit --skill agent-board --agent codex -g -y
```

Replace `codex` with another supported agent, or omit `--agent` and `-g` to let the CLI choose the project-level installation interactively.

Then ask the agent from any target repository:

```text
Use $agent-board to install the board into this repository.
```

The Skill invokes the bundled installer and guides the agent to register before editing, publish progress while working, and complete or release the task when leaving it.

## Install Directly Into A Project

The target directory must be a Git project root.

```powershell
git clone https://github.com/Evilom/agent-board-kit.git
python .\agent-board-kit\install.py D:\your-project
```

To share one board across linked worktrees from the same clone:

```powershell
python .\agent-board-kit\install.py D:\your-project --storage git-common
```

The idempotent installer:

- Writes `scripts/agent_board.py`.
- Writes `.agents/board/schema.json`.
- Adds a managed `.gitignore` block for runtime state and lock files.
- Adds managed coordination rules to `AGENTS.md` while preserving existing content.
- Optionally writes `.agents/board/config.json` for `git-common` storage.
- Runs `status --active` as an installation smoke test.

Run the same command again to upgrade. The installer refuses to overwrite an unrelated CLI unless `--force` is explicitly supplied. Use `--no-agents` when the project maintains its coordination rules elsewhere.

## Agent Workflow

```powershell
# Inspect active work and conflicts.
python scripts/agent_board.py status --active
python scripts/agent_board.py conflicts

# Register before editing. Use a stable agent ID.
python scripts/agent_board.py start --agent codex-auth-fix --tool codex `
  --task "Fix the login flow" --scope src/auth,tests --files src/auth/login.py

# Publish progress, blockers, and messages.
python scripts/agent_board.py update --agent codex-auth-fix --note "Reproduction complete"
python scripts/agent_board.py block --agent codex-auth-fix --blocker "Waiting for a test account" --handoff "Regression test added"
python scripts/agent_board.py message --agent codex-auth-fix --to all "Editing src/auth"

# Clear fields that no longer apply.
python scripts/agent_board.py update --agent codex-auth-fix --clear-files --clear-blockers

# Close or release the task.
python scripts/agent_board.py done --agent codex-auth-fix --note "Tests pass"
python scripts/agent_board.py release --agent codex-auth-fix --handoff "No business files changed"
```

`start` always creates a fresh task. It refuses to overwrite another open task for the same agent ID unless `--replace` is explicit. Mutating commands require an existing agent record, so a mistyped ID cannot silently create a new task.

Scope and file conflicts are advisory warnings. Inspect the overlapping task and coordinate with its owner before editing the same area.

## Diagnostics And Recovery

Every mutation validates `state.json` and preserves the previous valid state as `state.json.bak`. If state is damaged, writes fail closed instead of replacing it.

```powershell
python scripts/agent_board.py doctor
python scripts/agent_board.py doctor --json
python scripts/agent_board.py repair --from-backup
```

The damaged file is retained as `state.json.corrupt-*` before repair. Structured inspection and maintenance commands include:

```powershell
python scripts/agent_board.py status --active --json
python scripts/agent_board.py conflicts --json
python scripts/agent_board.py events --limit 20 --json
python scripts/agent_board.py sweep --stale-hours 4 --blocked-stale-hours 24
python scripts/agent_board.py compact --done-hours 72 --keep-messages 20
```

## Data Boundaries

- `state.json` is the current-state source of truth; `messages.jsonl` and `events.jsonl` are append-only records.
- `checkout` storage coordinates processes in one working directory.
- `git-common` storage coordinates linked worktrees from one clone and keeps state outside commits.
- Separate computers or clones remain independent. For cross-machine coordination, consume `events.jsonl` through an external service instead of committing runtime files.
- The board coordinates ownership; it does not replace Git diffs, tests, reviews, or commit history.

## Repository Files

| File | Purpose |
|---|---|
| `SKILL.md` | Agent Skill triggers and standard workflow |
| `agents/openai.yaml` | Codex display and invocation metadata |
| `agent_board.py` | Standalone CLI and single implementation source |
| `install.py` | Idempotent project installer and upgrader |
| `schema.json` | State, message, and event protocol |
| `AGENTS.snippet.md` | Coordination rules injected into target projects |
| `test_agent_board_kit.py` | Lifecycle, concurrency, standalone, worktree, and installer tests |

Run the test suite from the repository root:

```powershell
python -m unittest -v test_agent_board_kit.py
```

## Releases

Versioned changes follow semantic versioning. Every version bump receives a `vX.Y.Z` Git tag and a matching [GitHub Release](https://github.com/Evilom/agent-board-kit/releases).

## License

[MIT](LICENSE)
