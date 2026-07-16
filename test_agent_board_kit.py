import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


KIT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = KIT_DIR.parent.parent
CLI = KIT_DIR / "agent_board.py"


class AgentBoardKitTests(unittest.TestCase):
    def run_cli(
        self, root: Path, *args: str, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AGENT_BOARD_ROOT"] = str(root)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=check,
        )

    @staticmethod
    def read_state(root: Path) -> dict:
        return json.loads((root / ".agents" / "board" / "state.json").read_text(encoding="utf-8"))

    def test_standalone_cli_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()

            empty = self.run_cli(root, "status", "--active")
            self.assertIn("agent board: empty", empty.stdout)

            self.run_cli(
                root, "start", "--agent", "codex-demo", "--tool", "codex",
                "--task", "demo task", "--scope", "src,tests",
            )
            self.run_cli(
                root, "update", "--agent", "codex-demo",
                "--files", "src/main.py", "--note", "working",
            )
            self.run_cli(root, "message", "--agent", "codex-demo", "hello team")
            events = self.run_cli(root, "events", "--limit", "10", "--json")
            self.run_cli(root, "done", "--agent", "codex-demo", "--note", "verified")

            state = json.loads((root / ".agents" / "board" / "state.json").read_text(encoding="utf-8"))
            event_rows = [json.loads(line) for line in events.stdout.splitlines() if line.strip()]

        record = state["agents"]["codex-demo"]
        self.assertEqual("done", record["status"])
        self.assertEqual(["src", "tests"], record["scope"])
        self.assertEqual(["src/main.py"], record["files"])
        self.assertEqual("verified", record["note"])
        self.assertIn("agent.started", {row["type"] for row in event_rows})
        self.assertIn("message.sent", {row["type"] for row in event_rows})

    def test_concurrent_agents_do_not_lose_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            env = os.environ.copy()
            env["AGENT_BOARD_ROOT"] = str(root)
            processes = [
                subprocess.Popen(
                    [
                        sys.executable, str(CLI), "start", "--agent", f"agent-{index}",
                        "--tool", "test", "--task", f"task-{index}", "--scope", "src",
                    ],
                    cwd=root,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(12)
            ]
            results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
            state = json.loads((root / ".agents" / "board" / "state.json").read_text(encoding="utf-8"))

        self.assertTrue(all(returncode == 0 for _, _, returncode in results), results)
        self.assertEqual(12, len(state["agents"]))

    def test_new_start_is_fresh_and_mutations_require_an_existing_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.run_cli(
                root, "start", "--agent", "reused", "--task", "old task",
                "--scope", "old", "--files", "old.py", "--blocker", "old blocker",
                "--handoff", "old handoff", "--note", "old note",
            )
            first_started_at = self.read_state(root)["agents"]["reused"]["started_at"]
            self.run_cli(root, "done", "--agent", "reused")
            time.sleep(1.05)
            self.run_cli(root, "start", "--agent", "reused", "--task", "new task", "--scope", "new")
            record = self.read_state(root)["agents"]["reused"]

            self.assertNotEqual(first_started_at, record["started_at"])
            self.assertEqual([], record["files"])
            self.assertEqual([], record["blockers"])
            self.assertEqual("", record["handoff"])
            self.assertEqual("", record["note"])

            duplicate = self.run_cli(
                root, "start", "--agent", "reused", "--task", "duplicate", check=False,
            )
            self.assertNotEqual(0, duplicate.returncode)
            self.assertIn("already has an open task", duplicate.stderr)
            self.run_cli(root, "start", "--agent", "reused", "--task", "replacement", "--replace")

            missing = self.run_cli(root, "update", "--agent", "typo", "--note", "oops", check=False)
            self.assertNotEqual(0, missing.returncode)
            self.assertIn("has no board record", missing.stderr)
            self.assertNotIn("typo", self.read_state(root)["agents"])

    def test_update_can_clear_inherited_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.run_cli(
                root, "start", "--agent", "clear-demo", "--task", "demo",
                "--scope", "src", "--files", "src/a.py", "--blocker", "waiting",
                "--note", "old", "--handoff", "old",
            )
            self.run_cli(
                root, "update", "--agent", "clear-demo", "--clear-scope", "--clear-files",
                "--clear-blockers", "--clear-note", "--clear-handoff",
            )
            record = self.read_state(root)["agents"]["clear-demo"]

        self.assertEqual([], record["scope"])
        self.assertEqual([], record["files"])
        self.assertEqual([], record["blockers"])
        self.assertEqual("", record["note"])
        self.assertEqual("", record["handoff"])

    def test_corrupt_state_fails_closed_and_repair_restores_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.run_cli(root, "start", "--agent", "recover-me", "--task", "safe task")
            self.run_cli(root, "update", "--agent", "recover-me", "--note", "creates backup")
            state_path = root / ".agents" / "board" / "state.json"
            backup_path = root / ".agents" / "board" / "state.json.bak"
            self.assertTrue(backup_path.is_file())
            state_path.write_text("{broken\n", encoding="utf-8")

            status = self.run_cli(root, "status", "--active", check=False)
            self.assertNotEqual(0, status.returncode)
            self.assertIn("state.json is invalid", status.stderr)
            self.assertEqual("{broken\n", state_path.read_text(encoding="utf-8"))

            doctor = self.run_cli(root, "doctor", "--json", check=False)
            self.assertNotEqual(0, doctor.returncode)
            self.assertEqual("error", json.loads(doctor.stdout)["status"])

            self.run_cli(root, "repair", "--from-backup")
            repaired = self.read_state(root)
            corrupt_copies = list((root / ".agents" / "board").glob("state.json.corrupt-*"))

        self.assertIn("recover-me", repaired["agents"])
        self.assertEqual(1, len(corrupt_copies))

    def test_conflicts_warn_without_blocking_and_status_supports_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.run_cli(
                root, "start", "--agent", "agent-a", "--task", "auth",
                "--scope", "src/auth", "--files", "src/auth/login.py",
            )
            second = self.run_cli(
                root, "start", "--agent", "agent-b", "--task", "session",
                "--scope", "src/auth/session", "--files", "src/auth/session.py",
            )
            self.assertIn("claim conflict", second.stderr)
            self.assertIn("agent-a", second.stderr)

            conflicts = json.loads(self.run_cli(root, "conflicts", "--json").stdout)
            status = json.loads(self.run_cli(root, "status", "--active", "--json").stdout)

        self.assertEqual(1, len(conflicts["conflicts"]))
        self.assertTrue(conflicts["conflicts"][0]["scopes"])
        self.assertEqual(2, status["active_count"])
        self.assertEqual({"agent-a", "agent-b"}, {row["agent"] for row in status["agents"]})

    def test_blocked_records_use_a_longer_stale_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.run_cli(root, "start", "--agent", "active-old", "--task", "active")
            self.run_cli(root, "start", "--agent", "blocked-old", "--task", "blocked")
            self.run_cli(root, "block", "--agent", "blocked-old", "--blocker", "waiting")
            state = self.read_state(root)
            old_time = "2000-01-01 00:00:00"
            state["agents"]["active-old"]["updated_at"] = old_time
            state["agents"]["blocked-old"]["updated_at"] = old_time
            state_path = root / ".agents" / "board" / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            status = json.loads(self.run_cli(
                root, "status", "--active", "--json", "--stale-hours", "4",
                "--blocked-stale-hours", "1000000",
            ).stdout)

        self.assertEqual(["blocked-old"], [row["agent"] for row in status["agents"]])

    def test_mutating_commands_require_a_stable_agent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            env = os.environ.copy()
            env["AGENT_BOARD_ROOT"] = str(root)
            for key in ("AGENT_ID", "AGENT_NAME", "CODEX_AGENT_ID", "CLAUDE_AGENT_ID"):
                env.pop(key, None)
            result = subprocess.run(
                [sys.executable, str(CLI), "start", "--task", "missing id"],
                cwd=root,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("--agent", result.stderr)

    def test_git_common_storage_is_shared_between_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            worktree = base / "worktree"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "agent-board@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Agent Board Test"], cwd=root, check=True)
            subprocess.run(
                [sys.executable, str(KIT_DIR / "install.py"), str(root), "--no-agents", "--storage", "git-common"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            )
            (root / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "agent-board-test", str(worktree)],
                cwd=root,
                capture_output=True,
                check=True,
            )

            root_cli = root / "scripts" / "agent_board.py"
            worktree_cli = worktree / "scripts" / "agent_board.py"
            subprocess.run(
                [sys.executable, str(root_cli), "start", "--agent", "shared-agent", "--task", "shared"],
                cwd=root,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            )
            visible = subprocess.run(
                [sys.executable, str(worktree_cli), "status", "--active"],
                cwd=worktree,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            )

        self.assertIn("shared-agent", visible.stdout)

    def test_installer_is_idempotent_and_keeps_existing_project_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundle = base / "agent-board-kit"
            shutil.copytree(KIT_DIR, bundle, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            root = base / "target-project"
            root.mkdir()
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")

            for _ in range(2):
                subprocess.run(
                    [sys.executable, str(bundle / "install.py"), str(root)],
                    cwd=root,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=True,
                )

            agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
            ignore_text = (root / ".gitignore").read_text(encoding="utf-8")
            installed_cli = root / "scripts" / "agent_board.py"
            installed_schema = root / ".agents" / "board" / "schema.json"
            smoke = subprocess.run(
                [sys.executable, str(installed_cli), "status", "--active"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            )

            self.assertIn("# Existing rules", agents_text)
            self.assertEqual(1, agents_text.count("# >>> agent-board-kit >>>"))
            self.assertIn("build/", ignore_text)
            self.assertEqual(1, ignore_text.count("# >>> agent-board-kit >>>"))
            self.assertIn(".agents/board/state.json.bak", ignore_text)
            self.assertIn(".agents/board/state.json.corrupt-*", ignore_text)
            self.assertTrue(installed_cli.is_file())
            self.assertEqual((bundle / "agent_board.py").read_bytes(), installed_cli.read_bytes())
            self.assertEqual((bundle / "schema.json").read_bytes(), installed_schema.read_bytes())
            self.assertIn("agent board: empty", smoke.stdout)

    def test_installer_refuses_unrelated_cli_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            existing_cli = root / "scripts" / "agent_board.py"
            existing_cli.parent.mkdir()
            existing_cli.write_text("print('unrelated tool')\n", encoding="utf-8")

            refused = subprocess.run(
                [sys.executable, str(KIT_DIR / "install.py"), str(root), "--no-agents"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("refusing to overwrite unrelated file", refused.stderr)
            self.assertEqual("print('unrelated tool')\n", existing_cli.read_text(encoding="utf-8"))

            subprocess.run(
                [sys.executable, str(KIT_DIR / "install.py"), str(root), "--no-agents", "--force"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            )
            self.assertEqual(CLI.read_bytes(), existing_cli.read_bytes())

    def test_project_schema_and_compatibility_entry_stay_in_sync(self) -> None:
        self.assertEqual(
            (KIT_DIR / "schema.json").read_bytes(),
            (PROJECT_ROOT / ".agents" / "board" / "schema.json").read_bytes(),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "agent_board.py"), "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        self.assertIn("Shared agent bulletin board", result.stdout)
        version = subprocess.run(
            [sys.executable, str(CLI), "--version"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        self.assertEqual("agent-board-kit 1.1.0", version.stdout.strip())


if __name__ == "__main__":
    unittest.main()
