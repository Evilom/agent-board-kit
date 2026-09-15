"""Portable Agent Board durable execution ledger; never a queue for commands."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .common import NetworkError, digest, encoded, now


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise NetworkError("unsupported ledger version; refusing to change database")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(project, request_key));
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    time TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def create(self, project, key, request, actor, build):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_hash,data FROM tasks WHERE project=? AND request_key=?",
                             (project, key)).fetchone()
            if row:
                if row[0] != digest(request):
                    raise NetworkError("idempotency key already belongs to different input", 409)
                return json.loads(row[1]), False
            task_id = "task-" + uuid.uuid4().hex
            attempt_id = "attempt-" + uuid.uuid4().hex
            task = dict(request, task_id=task_id, attempt_id=attempt_id,
                        external_run_id=attempt_id, dag_name="ab-" + task_id[5:],
                        status="prepared", created_at=now(), updated_at=now(),
                        requested_by=actor, acceptance=None, evidence=None)
            task["dispatch"] = build(task)
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                       (task_id, project, key, digest(request), encoded(task).decode()))
            self._audit(db, task_id, actor, "prepared")
            return task, True

    @staticmethod
    def _audit(db, task_id, actor, event):
        db.execute("INSERT INTO audit(task_id,time,actor,event) VALUES(?,?,?,?)",
                   (task_id, now(), actor, event))

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise NetworkError("task not found", 404)
        return json.loads(row[0])

    def list(self, project):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM tasks WHERE project=? ORDER BY rowid DESC LIMIT 100", (project,))]

    def change(self, task_id, actor, event, change, allowed=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise NetworkError("task not found", 404)
            task = json.loads(row[0])
            if allowed is not None and task["status"] not in allowed:
                return task, False
            if change(task) is False:
                return task, False
            task["updated_at"] = now()
            db.execute("UPDATE tasks SET data=? WHERE id=?", (encoded(task).decode(), task_id))
            self._audit(db, task_id, actor, event)
            return task, True

    def events(self, task_id):
        with self.connect() as db:
            return [dict(zip(("seq", "time", "actor", "event"), row)) for row in db.execute(
                "SELECT seq,time,actor,event FROM audit WHERE task_id=? ORDER BY seq", (task_id,))]
