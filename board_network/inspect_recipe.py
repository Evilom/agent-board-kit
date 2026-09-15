"""Portable Agent Board git.inspect/v1: fixed read-only code sent to Dagu.

The caller prepends a JSON-decoded `contract`. No repository code is executed.
"""

import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone


def inspect(contract):
    started = datetime.now(timezone.utc).isoformat()
    env = os.environ.copy()
    # Do not inherit Git repository redirection from the worker's launch context.
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    root = contract["root"]

    def git(*args):
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if proc.returncode:
            raise RuntimeError("git inspection failed with exit code %d" % proc.returncode)
        if len(proc.stdout) > 1024 * 1024:
            raise RuntimeError("git inspection output exceeds limit")
        return proc.stdout

    receipt = {k: contract[k] for k in (
        "task_id", "attempt_id", "project_id", "workspace_id", "device_id", "environment_id", "expected_commit")}
    receipt.update(recipe="git.inspect/v1", started_at=started, actual_os=platform.system(),
                   hostname=platform.node(), exit_code=1, read_only=True)
    try:
        if platform.system() != contract["os"]:
            raise RuntimeError("worker OS differs from the approved target environment")
        actual_root = git("rev-parse", "--show-toplevel").decode().strip()
        if os.path.normcase(os.path.realpath(actual_root)) != os.path.normcase(os.path.realpath(root)):
            raise RuntimeError("mapped workspace is not the repository root")
        head_before = git("rev-parse", "--verify", "HEAD").decode().strip()
        status_before = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
        if head_before != contract["expected_commit"]:
            raise RuntimeError("target commit differs from requested input")
        head_after = git("rev-parse", "--verify", "HEAD").decode().strip()
        status_after = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
        if head_before != head_after or status_before != status_after:
            raise RuntimeError("workspace changed during inspection")
        receipt.update(exit_code=0, actual_commit=head_after, dirty=bool(status_after),
                       status_sha256=hashlib.sha256(status_after).hexdigest(),
                       status_entries=status_after.decode("utf-8", "replace").split("\0")[:200],
                       limitation="Metadata inspection only; no source snapshot, build or filesystem write lock.")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        receipt["error"] = str(exc)
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    print("AGENT_BOARD_RECEIPT=" + json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return receipt["exit_code"]
