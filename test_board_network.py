from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import tarfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from board_network.common import NetworkError, load_config, request_json
from board_network.dagu import Dagu
from board_network.hub import Hub, make_server
from board_network.inspect_recipe import inspect
from board_network.knowledge import search
from board_network.runtime import commands, install_runtime


KIT = Path(__file__).resolve().parent


class FakeDagu:
    """Emulates published Dagu response shapes and response-loss failures."""
    build = staticmethod(Dagu.build)

    def __init__(self):
        self.calls = 0
        self.task = None
        self.offline = False
        self.drop_ack = False

    def require_worker(self, workspace):
        if self.offline:
            raise NetworkError("worker unavailable", 409)

    def submit(self, task):
        self.calls += 1
        self.task = task
        if self.drop_ack:
            raise NetworkError("response lost", 502)

    def evidence(self, task):
        if self.offline:
            raise NetworkError("connection lost", 502)
        return "review", {"receipt": {"exit_code": 0}, "stdout": "kept", "log_complete": True}


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = self.root / "device.token"
        self.token.write_text("a" * 32)
        self.reader_token = self.root / "reader.token"
        self.reader_token.write_text("b" * 32)
        self.ws = {"device_id": "mac", "environment_id": "mac-native", "os": "Darwin", "root": "/approved/project", "python": "python3"}
        self.config = {"version": 1, "role": "server", "database": str(self.root / "board.sqlite3"),
                       "dagu": {"url": "http://127.0.0.1:8188"},
                       "projects": {"p": {"workspaces": {"mac": self.ws}}, "private": {"workspaces": {}}},
                       "principals": {"owner": {"device_id": "mac", "token_file": str(self.token), "grants": {
                           "p": {"operations": ["read", "query", "execute", "accept"], "workspaces": ["mac"]}}},
                           "reader": {"device_id": "pc", "token_file": str(self.reader_token), "grants": {
                               "p": {"operations": ["read", "query"], "workspaces": []}}}},
                       "knowledge_sources": {}}
        self.hub = Hub(self.config)
        self.fake = FakeDagu()
        self.hub.dagu = self.fake
        self.actor = self.hub.authenticate("Bearer " + self.token.read_text())
        self.reader = self.hub.authenticate("Bearer " + self.reader_token.read_text())
        self.request = {"project_id": "p", "workspace_id": "mac", "expected_commit": "a" * 40,
                        "idempotency_key": "one-execution", "title": "Read-only verification"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_requests_dispatch_once(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            tasks = list(pool.map(lambda _: self.hub.submit(self.actor, self.request), range(24)))
        self.assertEqual(1, self.fake.calls)
        self.assertEqual(1, len({t["task_id"] for t in tasks}))
        self.assertNotIn("dispatch", tasks[0])
        with self.assertRaisesRegex(NetworkError, "different input"):
            self.hub.submit(self.actor, dict(self.request, expected_commit="b" * 40))

    def test_ack_loss_restart_reconciles_without_resubmission(self):
        self.fake.drop_ack = True
        task = self.hub.submit(self.actor, self.request)
        self.assertEqual("unknown", task["status"])
        restarted = Hub(self.config)
        restarted.dagu = self.fake
        self.assertEqual("unknown", restarted.submit(self.actor, self.request)["status"])
        self.assertEqual("review", restarted.reconcile(self.actor, task["task_id"])["status"])
        self.assertEqual(1, self.fake.calls)
        with restarted.store.connect() as db:
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])

    def test_disconnect_preserves_evidence_and_never_marks_done(self):
        task = self.hub.submit(self.actor, self.request)
        self.fake.offline = True
        self.assertEqual("unknown", self.hub.reconcile(self.actor, task["task_id"])["status"])
        self.fake.offline = False
        t = self.hub.reconcile(self.actor, task["task_id"])
        self.assertEqual("review", t["status"])
        self.fake.offline = True
        t = self.hub.reconcile(self.actor, task["task_id"])
        self.assertEqual("review", t["status"])
        self.assertEqual("kept", t["evidence"]["stdout"])
        t = self.hub.accept(self.actor, task["task_id"], {"note": "Inspected receipt and log"})
        self.assertEqual("accepted", t["status"])
        self.assertEqual("accepted", self.hub.reconcile(self.actor, task["task_id"])["status"])

    def test_accept_requires_permission_and_complete_evidence(self):
        task = self.hub.submit(self.actor, self.request)
        with self.assertRaises(NetworkError):
            self.hub.accept(self.actor, task["task_id"], {"note": "process launched"})
        self.hub.reconcile(self.actor, task["task_id"])
        with self.assertRaises(NetworkError):
            self.hub.accept(self.reader, task["task_id"], {"note": "not authorized"})

    def test_offline_worker_is_prepared_and_never_falls_back_to_server(self):
        self.fake.offline = True
        task = self.hub.submit(self.actor, self.request)
        self.assertEqual("prepared", task["status"])
        self.assertEqual(0, self.fake.calls)
        self.fake.offline = False
        self.assertEqual("queued", self.hub.submit(self.actor, self.request)["status"])
        self.assertEqual(1, self.fake.calls)

    def test_project_and_workspace_permissions_and_injection(self):
        for request in (dict(self.request, project_id="private"), dict(self.request, workspace_id="other"),
                        dict(self.request, command="rm"), dict(self.request, expected_commit="$(whoami)")):
            with self.assertRaises(NetworkError):
                self.hub.submit(self.actor, request)
        with self.assertRaises(NetworkError):
            self.hub.submit(self.reader, self.request)
        for operation in ("read", "query", "execute", "accept"):
            with self.assertRaises(NetworkError):
                self.hub.authorize(self.actor, "private", operation)
        self.assertEqual(0, self.fake.calls)

    def test_real_http_auth_invalid_json_and_project_filtering(self):
        with make_server(self.config, ("127.0.0.1", 0)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = {"url": "http://127.0.0.1:%d" % server.server_port}
                self.assertTrue(request_json(endpoint, "/health")["ok"])
                with self.assertRaises(NetworkError) as error:
                    request_json(endpoint, "/v1/projects")
                self.assertEqual(401, error.exception.status)
                endpoint["token_file"] = str(self.token)
                self.assertEqual(["p"], [x["project_id"] for x in request_json(endpoint, "/v1/projects")["projects"]])
                with self.assertRaises(NetworkError) as error:
                    request_json(endpoint, "/v1/tasks?project_id=private")
                self.assertEqual(403, error.exception.status)
                with self.assertRaises(NetworkError):
                    request_json(endpoint, "/v1/tasks", [])
            finally:
                server.shutdown()
                thread.join()

    def test_source_version_refresh_deletion_scope_and_symlinks(self):
        docs = self.root / "docs"
        docs.mkdir()
        allowed = docs / "public"
        allowed.mkdir()
        file = allowed / "设计.md"
        file.write_text("# 中文资料\n跨设备协作 第一版\n", encoding="utf-8")
        (docs / "private.md").write_text("跨设备协作 私密")
        outside = self.root / "outside.md"
        outside.write_text("跨设备协作 越界")
        (allowed / "escape.md").symlink_to(outside)
        (allowed / "inside-escape.md").symlink_to(docs / "private.md")
        sources = {"docs": {"kind": "documents", "root": str(docs), "prefixes": ["public"], "projects": ["p"]}}
        first = search(sources, "p", "跨设备协作", 5)
        self.assertEqual(1, len(first["results"]))
        self.assertEqual([], search(sources, "private", "跨设备协作", 5)["results"])
        file.write_text("# 中文资料\n跨设备协作 第二版\n", encoding="utf-8")
        second = search(sources, "p", "跨设备协作", 5)
        self.assertNotEqual(first["results"][0]["version"], second["results"][0]["version"])
        self.assertIn("第二版", second["results"][0]["snippet"])
        file.unlink()
        self.assertEqual([], search(sources, "p", "跨设备协作", 5)["results"])

    def test_qmd_filters_traversal_and_drops_stale_snippet(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "real.md").write_text("# Current\nnew content 中文检索")
        source = {"q": {"kind": "qmd-http", "root": str(docs), "prefixes": ["real.md"],
                         "projects": ["p"], "collection": "wiki", "url": "http://127.0.0.1:8765"}}
        hits = {"ok": True, "results": [
            {"file": "qmd://wiki/real.md", "docid": "#old", "snippet": "stale content"},
            {"file": "qmd://wiki/%2e%2e/outside.md"}, {"file": "qmd://private/real.md"}]}
        with patch("board_network.knowledge.request_json", return_value=hits):
            result = search(source, "p", "中文检索", 5)
            self.assertEqual(1, len(result["results"]))
            self.assertNotIn("stale", result["results"][0]["snippet"])
            self.assertEqual("#old", result["results"][0]["index_version"])
            (docs / "real.md").unlink()
            self.assertEqual([], search(source, "p", "中文检索", 5)["results"])
        with patch("board_network.knowledge.request_json", side_effect=NetworkError("offline", 502)):
            self.assertEqual("unavailable", search(source, "p", "test", 5)["sources"][0]["status"])

    def real_contract(self):
        task = self.hub.submit(self.actor, self.request)
        receipt = {k: task[k] for k in ("task_id", "attempt_id", "project_id", "workspace_id", "expected_commit")}
        receipt.update({k: task["target"][k] for k in ("device_id", "environment_id")})
        receipt.update(actual_os="Darwin", actual_commit="a" * 40, recipe="git.inspect/v1", exit_code=0,
                       finished_at="2026-09-15T00:00:00Z", read_only=True)
        run = {"dagRunDetails": {"dagRunId": task["external_run_id"], "name": task["dag_name"],
                                 "statusLabel": "succeeded", "workerId": "mac"}}
        return task, receipt, run

    def test_dagu_published_response_and_missing_evidence(self):
        task, receipt, run = self.real_contract()
        adapter = Dagu(self.config["dagu"])
        for mode in ("valid", "wrong-device", "wrong-commit", "partial-log", "no-receipt", "missing-worker"):
            r = dict(receipt)
            data = copy.deepcopy(run)
            log = {"lineCount": 1, "totalLines": 1}
            if mode == "wrong-device": r["device_id"] = "other"
            if mode == "wrong-commit": r["actual_commit"] = "b" * 40
            if mode == "partial-log": log["totalLines"] = 2
            if mode == "missing-worker": data["dagRunDetails"].pop("workerId")
            log["content"] = "AGENT_BOARD_RECEIPT=" + json.dumps(r) if mode != "no-receipt" else "process exited 0"
            with self.subTest(mode=mode), patch("board_network.dagu.request_json", side_effect=[data, log]):
                status, _ = adapter.evidence(task)
                self.assertEqual("review" if mode == "valid" else "unknown", status)

    def test_worker_health_and_native_os_must_match(self):
        adapter = Dagu(self.config["dagu"])
        worker = {"id": "mac", "healthStatus": "healthy", "labels": {"board_device": "mac", "board_environment": "mac-native", "os": "darwin"}}
        with patch.object(adapter, "workers", return_value=[worker]):
            self.assertEqual("darwin", adapter.require_worker(self.ws)["os"])
            worker["labels"]["os"] = "linux"
            with self.assertRaises(NetworkError): adapter.require_worker(self.ws)

    def test_read_only_recipe_preserves_dirty_files_and_git_index(self):
        root = self.root / "repo with spaces"
        root.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()
        git("init")
        (root / "a.txt").write_text("committed")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        head = git("rev-parse", "HEAD")
        (root / "a.txt").write_text("uncommitted change")
        (root / "untracked.txt").write_text("preserve")
        before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
        contract = dict(task_id="t", attempt_id="a", project_id="p", workspace_id="w", device_id="d", environment_id="e",
                        root=str(root), expected_commit=head, os=platform.system())
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(0, inspect(contract))
        self.assertIn('"dirty": true', stream.getvalue())
        after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(1, inspect(dict(contract, expected_commit="b" * 40)))
            self.assertEqual(1, inspect(dict(contract, os="WrongOS")))

    def test_client_role_starts_only_worker_and_requires_remote_tls(self):
        runtime = self.root / "runtime"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin" / ("dagu.exe" if os.name == "nt" else "dagu")).write_text("binary placeholder")
        cfg = {"role": "client", "runtime_dir": str(runtime), "worker": {
            "device_id": "windows", "environment_id": "windows-native", "coordinator": "127.0.0.1:51855", "peer_insecure": True}}
        jobs = commands(cfg, "client.json")
        self.assertEqual(["worker"], [name for name, _ in jobs])
        cfg["worker"]["coordinator"] = "192.168.1.199:51855"
        with self.assertRaisesRegex(NetworkError, "mTLS"):
            commands(cfg, "client.json")

    def test_optional_install_keeps_offline_cli_working(self):
        root = self.root / "installed"
        root.mkdir()
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        subprocess.run([sys.executable, str(KIT / "install.py"), str(root), "--network", "--no-agents"], check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(root / "scripts/agent_board.py"), "status", "--json"],
                                cwd=root, check=True, capture_output=True, text=True)
        self.assertEqual(1, json.loads(result.stdout)["version"])
        self.assertTrue((root / "scripts/board_network/hub.py").is_file())
        self.assertIn('.runtime/', (root / '.gitignore').read_text())
        cfg = root / "local" / "config.json"
        subprocess.run([sys.executable, str(root / "scripts/agent_board.py"), "network", "--config", str(cfg),
                        "init-server", "--project", "p", "--root", str(root)], check=True, capture_output=True)
        self.assertEqual("server", load_config(cfg)["role"])
        again = subprocess.run([sys.executable, str(root / "scripts/agent_board.py"), "network", "--config", str(cfg),
                                "init-server", "--project", "p"], capture_output=True)
        self.assertNotEqual(0, again.returncode)

    def test_windows_runtime_uses_published_tar_and_verifies_checksum(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            item = tarfile.TarInfo("dagu.exe")
            item.size = 8
            archive.addfile(item, io.BytesIO(b"test-exe"))
        data = buffer.getvalue()
        checksums = (hashlib.sha256(data).hexdigest() + "  dagu_2.16.6_windows_amd64.tar.gz\n").encode()
        config = {"runtime_dir": str(self.root / "win-runtime")}
        def download(url, **kwargs):
            self.assertNotIn('.zip', url)
            return io.BytesIO(checksums if url.endswith('checksums.txt') else data)
        with patch('board_network.runtime.platform.system', return_value='Windows'), \
             patch('board_network.runtime.platform.machine', return_value='AMD64'), \
             patch('board_network.runtime.urlopen', side_effect=download):
            result = install_runtime(config)
        self.assertEqual(b'test-exe', Path(result['binary']).read_bytes())
        self.assertEqual('dagu.exe', Path(result['binary']).name)

    def test_pair_exports_client_only_and_backs_up_server_config(self):
        config_path = self.root / 'server' / 'config.json'
        def cli(*args):
            return subprocess.run([sys.executable, str(KIT / 'agent_board.py'), 'network', '--config', str(config_path), *args],
                                  check=True, text=True, capture_output=True)
        cli('init-server', '--project', 'p', '--root', str(self.root))
        before = config_path.read_bytes()
        result = json.loads(cli('pair', '--project', 'p', '--device', 'windows-main', '--environment', 'windows-native',
                               '--workspace', 'win', '--os', 'Windows', '--root', 'D:/projects/p',
                               '--hub-url', 'https://mac.example:8940', '--coordinator', 'mac.example:51855').stdout)
        self.assertEqual(before, Path(result['backup']).read_bytes())
        server = load_config(config_path)
        client = load_config(result['client_config'])
        self.assertEqual('client', client['role'])
        self.assertNotIn('database', client)
        self.assertEqual('D:/projects/p', server['projects']['p']['workspaces']['win']['root'])
        self.assertNotIn('accept', server['principals']['windows-main']['grants']['p']['operations'])
        self.assertNotEqual(server['principals']['windows-main']['token_file'], server['principals']['mac-local']['token_file'])


if __name__ == "__main__":
    unittest.main()
