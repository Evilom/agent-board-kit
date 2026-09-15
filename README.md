# Agent Board Kit

English | [简体中文](README.zh-CN.md)

Agent Board coordinates agents across computers around shared tasks, messages, handoffs, evidence, and existing knowledge. The main computer runs the server and a local client; secondary computers run clients only.

Network component **0.2.0** adds a Chinese web client, Codex / Claude Code launch adapters, 15 MCP tools, persistent task ownership and messages, device heartbeats, and optional Dagu execution. Local Board **1.1.1** remains compatible. Python 3.9+ is sufficient for network collaboration; no Git repository or execution engine is required to create tasks or exchange messages.

```sh
python3 agent_board.py network --config .runtime/config.json init-server --project my-work --device mac-main --root /path/to/work
python3 agent_board.py network --config .runtime/config.json service
# In another terminal:
python3 agent_board.py network --config .runtime/config.json open
```

Read the [Chinese setup and Windows update guide](docs/跨设备使用指南.md) and [validation record](docs/升级验收记录-0.2.md). Windows hardware validation remains pending. Dagu runs as a separate, optional process; existing knowledge stays at its source. Git distributes program updates.

## Local Board compatibility

The following sections describe the original local mode, which works without a server, database, Node.js, or third-party Python packages.

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
- Separate computers or clones keep their local boards independent. The optional network component uses a separate durable ledger; `events.jsonl` is observational and must not be used as a reliable execution queue.
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
python -m unittest -v test_agent_board_kit test_board_network test_collaboration test_network_runtime
```

## Releases

Local and network components carry separate versions. Published archives are available in [GitHub Release](https://github.com/Evilom/agent-board-kit/releases).

## License

[MIT](LICENSE)
