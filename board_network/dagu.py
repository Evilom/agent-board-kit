"""Portable Agent Board adapter for Dagu REST API, verified with v2.16.6."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from .common import NetworkError, now, request_json


class Dagu:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def workers(self):
        data = request_json(self.endpoint, "/api/v1/workers")
        if not isinstance(data, dict) or not isinstance(data.get("workers"), list) or data.get("errors"):
            raise NetworkError("Dagu worker discovery unavailable", 502)
        return data["workers"]

    def require_worker(self, workspace):
        expected = {"board_device": workspace["device_id"],
                    "board_environment": workspace["environment_id"],
                    "os": {"Darwin": "darwin", "Windows": "windows", "Linux": "linux"}[workspace["os"]]}
        for worker in self.workers():
            labels = worker.get("labels", {})
            if (worker.get("id") == workspace["device_id"] and worker.get("healthStatus") == "healthy"
                    and all(labels.get(k) == v for k, v in expected.items())):
                return expected
        raise NetworkError("target worker is offline or its approved environment does not match", 409)

    @staticmethod
    def build(task, workspace):
        contract = {key: task[key] for key in (
            "task_id", "attempt_id", "project_id", "workspace_id", "expected_commit")}
        contract.update({key: workspace[key] for key in ("device_id", "environment_id", "root", "os")})
        payload = base64.b64encode(json.dumps(contract).encode()).decode()
        script = ("import base64, json\ncontract = json.loads(base64.b64decode(%r))\n" % payload
                  + Path(__file__).with_name("inspect_recipe.py").read_text(encoding="utf-8")
                  + "\nraise SystemExit(inspect(contract))\n")
        selector = {"board_device": workspace["device_id"], "board_environment": workspace["environment_id"],
                    "os": {"Darwin": "darwin", "Windows": "windows", "Linux": "linux"}[workspace["os"]]}
        spec = {"worker_selector": selector, "timeout_sec": 120,
                "steps": [{"name": "inspect", "shell": workspace.get("python", "python3"), "script": script}]}
        return {"name": task["dag_name"], "dagRunId": task["external_run_id"], "spec": json.dumps(spec)}

    def submit(self, task):
        result = request_json(self.endpoint, "/api/v1/dag-runs/enqueue", task["dispatch"])
        if not isinstance(result, dict) or result.get("dagRunId") != task["external_run_id"]:
            raise NetworkError("Dagu returned a different run ID; reconcile before further action", 502)

    def evidence(self, task):
        path = "/api/v1/dag-runs/%s/%s" % (quote(task["dag_name"], safe=""), quote(task["external_run_id"], safe=""))
        data = request_json(self.endpoint, path)
        if not isinstance(data, dict):
            raise NetworkError("Dagu returned invalid run details", 502)
        run = data.get("dagRunDetails", data.get("dagRun", {}))
        if not isinstance(run, dict) or run.get("dagRunId") != task["external_run_id"] or run.get("name") != task["dag_name"]:
            raise NetworkError("Dagu run identity mismatch", 502)
        label = str(run.get("statusLabel", "unknown")).lower()
        if label in ("queued", "not started", "not_started"):
            return "queued", {"backend_status": label, "observed_at": now()}
        if label in ("running", "waiting"):
            return "running", {"backend_status": label, "observed_at": now()}
        if label not in ("succeeded", "success", "failed", "aborted", "cancelled", "rejected"):
            return "unknown", {"backend_status": label, "observed_at": now()}
        log = request_json(self.endpoint, path + "/steps/inspect/log?stream=stdout&limit=10000")
        if not isinstance(log, dict) or not isinstance(log.get("content"), str):
            raise NetworkError("Dagu returned invalid log evidence", 502)
        content = log.get("content", "")
        complete = log.get("hasMore") is False or (
            "hasMore" not in log and type(log.get("lineCount")) is int
            and log["lineCount"] == log.get("totalLines") and log["lineCount"] > 0)
        evidence = {"backend_status": label, "observed_at": now(), "stdout": content,
                    "stdout_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "log_complete": complete, "receipt": None,
                    "worker_id": run.get("workerId"), "started_at": run.get("startedAt"),
                    "finished_at": run.get("finishedAt")}
        lines = [line.split("AGENT_BOARD_RECEIPT=", 1)[1] for line in content.splitlines()
                 if line.startswith("AGENT_BOARD_RECEIPT=")]
        if len(lines) == 1:
            try:
                receipt = json.loads(lines[0])
                expected = {k: task[k] for k in ("task_id", "attempt_id", "project_id", "workspace_id", "expected_commit")}
                expected.update(task["target"])
                identity = all(receipt.get(k) == v for k, v in expected.items() if k != "os")
                identity = identity and receipt.get("actual_os") == expected["os"]
                if identity and receipt.get("recipe") == "git.inspect/v1":
                    evidence["receipt"] = receipt
            except (ValueError, TypeError, AttributeError):
                pass
        receipt = evidence["receipt"]
        valid = (receipt is not None and type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0
                 and receipt.get("actual_commit") == task["expected_commit"]
                 and receipt.get("read_only") is True and receipt.get("finished_at")
                 and evidence["log_complete"] and evidence["worker_id"] == task["target"]["device_id"])
        if label in ("succeeded", "success"):
            return ("review" if valid else "unknown"), evidence
        return "failed", evidence
