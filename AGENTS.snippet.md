## Multi-Agent Board

Before changing files, inspect active work and register the task:

```bash
python scripts/agent_board.py status --active
python scripts/agent_board.py start --agent <stable-id> --tool <tool> --task "<one-line task>" --scope <paths-or-domain>
```

- Use a stable agent ID for the whole task.
- `start` always creates a fresh task. Use `update` for an open task; use `--replace` only when intentionally discarding it.
- Add `--files` when the expected files are known.
- Treat automatic claim-conflict warnings as a prompt to coordinate, not as a hard lock.
- Update long tasks every 30-60 minutes with `update --note "<progress>"`.
- Use `block` for a real blocker and include `--handoff` when another agent can continue; blocked tasks have a longer stale window.
- End every task with `done` or `release`; do not leave abandoned active records.
- Do not overwrite another active agent's scope without coordinating through `message`.
- Run `python scripts/agent_board.py doctor` when state or lock errors appear; never edit or discard runtime state blindly.
- The board coordinates work but does not replace Git diff review, tests, or commits.
