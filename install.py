#!/usr/bin/env python3
"""Install Portable Agent Board into another Git project."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


KIT_DIR = Path(__file__).resolve().parent
BEGIN_MARKER = "# >>> agent-board-kit >>>"
END_MARKER = "# <<< agent-board-kit <<<"
GITIGNORE_BODY = """# Runtime state is local to one checkout.
.agents/board/state.json
.agents/board/messages.jsonl
.agents/board/events.jsonl
.agents/board/.lock
.agents/board/*.tmp
.agents/board/state.json.bak
.agents/board/state.json.corrupt-*"""


def managed_text(existing: str, body: str) -> str:
    block = f"{BEGIN_MARKER}\n{body.rstrip()}\n{END_MARKER}"
    has_begin = BEGIN_MARKER in existing
    has_end = END_MARKER in existing
    if has_begin != has_end:
        raise RuntimeError("managed agent-board-kit block is incomplete")
    if has_begin:
        start = existing.index(BEGIN_MARKER)
        end = existing.index(END_MARKER, start) + len(END_MARKER)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].strip()
        parts = [part for part in (prefix, block, suffix) if part]
        return "\n\n".join(parts) + "\n"
    prefix = existing.rstrip()
    return ((prefix + "\n\n") if prefix else "") + block + "\n"


def update_managed_file(path: Path, body: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = managed_text(existing, body)
    if updated != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)


def install_owned_file(source: Path, target: Path, signature: str, force: bool) -> None:
    source_bytes = source.read_bytes()
    if target.exists():
        existing = target.read_bytes()
        if existing == source_bytes:
            return
        if not force and signature.encode("utf-8") not in existing:
            raise RuntimeError(
                f"refusing to overwrite unrelated file: {target}; rerun with --force after review"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def update_storage_config(target_root: Path, storage: str) -> None:
    path = target_root / ".agents" / "board" / "config.json"
    config = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"agent board config is invalid: {path}: {exc}")
        if not isinstance(config, dict):
            raise RuntimeError(f"agent board config must be a JSON object: {path}")
    config.update({"version": 1, "storage": storage})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def install(target_root: Path, force: bool, include_agents: bool, storage: str = "") -> None:
    target_root = target_root.expanduser().resolve()
    if not target_root.is_dir():
        raise RuntimeError(f"target project does not exist: {target_root}")
    if not (target_root / ".git").exists():
        raise RuntimeError(f"target is not a Git project: {target_root}")

    installed_cli = target_root / "scripts" / "agent_board.py"
    installed_schema = target_root / ".agents" / "board" / "schema.json"
    install_owned_file(KIT_DIR / "agent_board.py", installed_cli, "Portable Agent Board", force)
    install_owned_file(KIT_DIR / "schema.json", installed_schema, "Portable Agent Board protocol", force)
    update_managed_file(target_root / ".gitignore", GITIGNORE_BODY)
    if include_agents:
        snippet = (KIT_DIR / "AGENTS.snippet.md").read_text(encoding="utf-8")
        update_managed_file(target_root / "AGENTS.md", snippet)
    if storage:
        update_storage_config(target_root, storage)

    env = os.environ.copy()
    env["AGENT_BOARD_ROOT"] = str(target_root)
    smoke = subprocess.run(
        [sys.executable, str(installed_cli), "status", "--active"],
        cwd=target_root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(f"installed CLI smoke test failed: {smoke.stderr.strip() or smoke.stdout.strip()}")

    config_path = target_root / ".agents" / "board" / "config.json"
    effective_storage = "checkout"
    if config_path.exists():
        effective_storage = json.loads(config_path.read_text(encoding="utf-8")).get("storage", "checkout")

    print(f"agent-board-kit installed: {target_root}")
    print(f"  cli: {installed_cli.relative_to(target_root)}")
    print(f"  schema: {installed_schema.relative_to(target_root)}")
    print(f"  storage: {effective_storage}")
    print("  check: python scripts/agent_board.py status --active")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Portable Agent Board into a Git project")
    parser.add_argument("target", help="Target Git project root")
    parser.add_argument("--force", action="store_true", help="Replace conflicting CLI or schema after review")
    parser.add_argument("--no-agents", action="store_true", help="Do not update target AGENTS.md")
    parser.add_argument(
        "--storage", choices=("checkout", "git-common"), default="",
        help="Persist storage mode; git-common shares one board across linked worktrees",
    )
    args = parser.parse_args()
    try:
        install(
            Path(args.target),
            force=args.force,
            include_agents=not args.no_agents,
            storage=args.storage,
        )
    except (OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
