"""Verify a live server/client round trip without executing project code."""

import argparse
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from board_network.common import NetworkError, load_config, now, request_json


p = argparse.ArgumentParser()
p.add_argument("--config", required=True)
p.add_argument("--project", required=True)
p.add_argument("--workspace", required=True)
p.add_argument("--commit", required=True)
p.add_argument("--query", default="协作")
p.add_argument("--output", required=True)
args = p.parse_args()
endpoint = load_config(args.config)["hub"]
body = {"project_id": args.project, "workspace_id": args.workspace, "expected_commit": args.commit,
        "title": "Read-only server/client acceptance check", "idempotency_key": "smoke-" + secrets.token_hex(8)}
task = request_json(endpoint, "/v1/tasks", body)
again = request_json(endpoint, "/v1/tasks", body)
assert task["task_id"] == again["task_id"] and task["external_run_id"] == again["external_run_id"]
deadline = time.monotonic() + 40
while task["status"] not in ("review", "failed", "accepted") and time.monotonic() < deadline:
    time.sleep(0.5)
    task = request_json(endpoint, "/v1/tasks/" + task["task_id"] + "/reconcile", {})
assert task["status"] == "review", task
receipt = task["evidence"]["receipt"]
assert receipt["actual_commit"] == args.commit and receipt["exit_code"] == 0
assert hashlib.sha256(task["evidence"]["stdout"].encode()).hexdigest() == task["evidence"]["stdout_sha256"]
accepted = request_json(endpoint, "/v1/tasks/" + task["task_id"] + "/accept", {"note": "Verified native worker, commit, read-only receipt and log hash in live smoke check."})
assert accepted["status"] == "accepted"
knowledge = request_json(endpoint, "/v1/knowledge/search", {"project_id": args.project, "query": args.query})
assert knowledge["results"], knowledge
try:
    request_json(endpoint, "/v1/knowledge/search", {"project_id": "ungranted-project", "query": args.query})
    raise AssertionError("cross-project query was allowed")
except NetworkError as exc:
    assert exc.status == 403
report = {"verified_at": now(), "task_id": task["task_id"], "external_run_id": task["external_run_id"],
          "worker": task["evidence"]["worker_id"], "actual_os": receipt["actual_os"],
          "commit": receipt["actual_commit"], "exit_code": receipt["exit_code"],
          "stdout_sha256": task["evidence"]["stdout_sha256"], "status": accepted["status"],
          "request_deduplicated": True, "cross_project_denied": True,
          "knowledge": [{k: item[k] for k in ("source_id", "uri", "version", "freshness")} for item in knowledge["results"]]}
Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
