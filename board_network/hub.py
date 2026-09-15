"""Portable Agent Board server: authorization, durable task ownership and evidence."""

from __future__ import annotations

import hmac
import json
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import VERSION
from .common import NetworkError, identifier, loopback, now, read_token
from .dagu import Dagu
from .knowledge import search
from .store import Store


OPERATIONS = {"read", "query", "execute", "accept"}


def public_task(task):
    return {k: v for k, v in task.items() if k != "dispatch"}


class Hub:
    def __init__(self, config):
        self.config = config
        self.validate()
        self.store = Store(config["database"])
        self.dagu = Dagu(config["dagu"])

    def validate(self):
        projects = self.config.get("projects", {})
        for project_id, project in projects.items():
            identifier(project_id)
            for workspace_id, ws in project.get("workspaces", {}).items():
                identifier(workspace_id)
                identifier(ws["device_id"])
                identifier(ws["environment_id"])
                if ws["os"] not in ("Darwin", "Windows", "Linux"):
                    raise NetworkError("workspace os must be Darwin, Windows or Linux")
                if not isinstance(ws["root"], str) or not ws["root"]:
                    raise NetworkError("workspace root is required")
        tokens = set()
        for principal_id, principal in self.config.get("principals", {}).items():
            identifier(principal_id)
            identifier(principal["device_id"])
            token = read_token(principal["token_file"])
            if token in tokens:
                raise NetworkError("each principal must have a distinct token")
            tokens.add(token)
            for project_id, grant in principal.get("grants", {}).items():
                if project_id not in projects or not set(grant.get("operations", [])) <= OPERATIONS:
                    raise NetworkError("invalid project grant")
                if not set(grant.get("workspaces", [])) <= set(projects[project_id].get("workspaces", {})):
                    raise NetworkError("grant references unknown workspace")
        if not tokens:
            raise NetworkError("at least one authenticated principal is required")
        for source in self.config.get("knowledge_sources", {}).values():
            if not set(source.get("projects", [])) <= set(projects):
                raise NetworkError("knowledge source references unknown project")
            if source.get("kind") not in ("documents", "qmd-http") or not source.get("prefixes"):
                raise NetworkError("knowledge source requires provider and explicit prefixes")
            for prefix in source["prefixes"]:
                if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or ".." in prefix.split("/") or "\\" in prefix:
                    raise NetworkError("knowledge prefixes must be safe relative paths")

    def authenticate(self, header):
        if not header.startswith("Bearer "):
            raise NetworkError("authentication required", 401)
        supplied = header[7:]
        for principal_id, principal in self.config["principals"].items():
            if not principal.get("disabled", False) and hmac.compare_digest(supplied, read_token(principal["token_file"])):
                return principal_id, principal
        raise NetworkError("authentication required", 401)

    def authorize(self, actor, project_id, operation, workspace_id=None):
        _, principal = actor
        grant = principal.get("grants", {}).get(project_id, {})
        if operation not in grant.get("operations", []):
            raise NetworkError("project operation is not granted", 403)
        if workspace_id and workspace_id not in grant.get("workspaces", []):
            raise NetworkError("target workspace is not granted", 403)

    def project_list(self, actor):
        result = []
        for project_id, project in self.config["projects"].items():
            grant = actor[1].get("grants", {}).get(project_id, {})
            if not grant:
                continue
            result.append({"project_id": project_id, "name": project.get("name", project_id),
                           "operations": grant["operations"], "workspaces": [
                               dict(workspace_id=wid, **{k: ws[k] for k in ("device_id", "environment_id", "os")})
                               for wid, ws in project.get("workspaces", {}).items() if wid in grant.get("workspaces", [])]})
        return {"projects": result}

    def submit(self, actor, body):
        fields = {"project_id", "workspace_id", "title", "expected_commit", "idempotency_key"}
        if set(body) != fields or not all(isinstance(body[k], str) and body[k].strip() for k in fields):
            raise NetworkError("task requires only project_id, workspace_id, title, expected_commit, idempotency_key")
        project_id, workspace_id = identifier(body["project_id"]), identifier(body["workspace_id"])
        self.authorize(actor, project_id, "execute", workspace_id)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", body["expected_commit"]):
            raise NetworkError("expected_commit must be a full lowercase Git commit hash")
        if len(body["title"]) > 500:
            raise NetworkError("title is too long")
        key = identifier(body["idempotency_key"])
        workspace = self.config["projects"][project_id]["workspaces"][workspace_id]
        request = {k: body[k] for k in fields if k != "idempotency_key"}
        request.update(recipe="git.inspect/v1", scope=["."], mode="read-only-metadata",
                       target={k: workspace[k] for k in ("device_id", "environment_id", "os")})
        task, _ = self.store.create(project_id, key, request, actor[0], lambda t: self.dagu.build(t, workspace))
        # A prepared task has never crossed the execution boundary. Every later
        # state is reconciled only; no timeout/restart can call submit a second time.
        if task["status"] != "prepared":
            return public_task(task)
        try:
            self.dagu.require_worker(workspace)
        except NetworkError as exc:
            task, _ = self.store.change(task["task_id"], actor[0], "worker.unavailable",
                                        lambda t: t.update(last_error=str(exc)), {"prepared"})
            return public_task(task)
        task, owned = self.store.change(task["task_id"], actor[0], "dispatch.started",
                                       lambda t: t.update(status="submitting", last_error=None), {"prepared"})
        if owned:
            try:
                self.dagu.submit(task)
                task, _ = self.store.change(task["task_id"], actor[0], "dispatch.acknowledged",
                                           lambda t: t.update(status="queued", last_error=None), {"submitting", "unknown"})
            except NetworkError as exc:
                task, _ = self.store.change(task["task_id"], actor[0], "dispatch.uncertain",
                                           lambda t: t.update(status="unknown", last_error=str(exc)), {"submitting"})
        return public_task(task)

    def task_for(self, actor, task_id, operation):
        task = self.store.get(identifier(task_id))
        self.authorize(actor, task["project_id"], operation)
        return task

    def reconcile(self, actor, task_id):
        task = self.task_for(actor, task_id, "read")
        if task["status"] in ("accepted", "prepared", "review", "failed"):
            return public_task(task)
        try:
            status, evidence = self.dagu.evidence(task)
            changes = dict(status=status, evidence=evidence, last_error=None)
        except NetworkError as exc:
            # Keep already stored terminal evidence during an outage.
            changes = dict(status=task["status"] if task["status"] in ("review", "failed") else "unknown",
                           last_error=str(exc))
        # A stale reconciliation must not overwrite a newer result or acceptance.
        revision = task["updated_at"]
        def update(current):
            if current["updated_at"] == revision:
                current.update(changes)
            else:
                return False
        task, _ = self.store.change(task_id, actor[0], "execution.reconciled", update,
                                    {"submitting", "unknown", "queued", "running", "review", "failed"})
        return public_task(task)

    def accept(self, actor, task_id, body):
        self.task_for(actor, task_id, "accept")
        note = body.get("note")
        if set(body) != {"note"} or not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise NetworkError("acceptance requires a review note (1-2000 characters)")
        task, changed = self.store.change(task_id, actor[0], "task.accepted", lambda t: t.update(
            status="accepted", acceptance={"actor": actor[0], "note": note, "accepted_at": now()}), {"review"})
        if not changed:
            raise NetworkError("task has no complete successful evidence awaiting acceptance", 409)
        return public_task(task)

    def route(self, actor, method, path, query, body):
        if method == "GET" and path == "/v1/projects":
            return self.project_list(actor)
        if method == "GET" and path == "/v1/tasks":
            project = (query.get("project_id") or [""])[0]
            self.authorize(actor, project, "read")
            return {"tasks": [public_task(t) for t in self.store.list(project)]}
        if method == "POST" and path == "/v1/tasks":
            return self.submit(actor, body)
        if method == "POST" and path == "/v1/knowledge/search":
            if set(body) - {"project_id", "query", "limit"}:
                raise NetworkError("unexpected search fields")
            project = body.get("project_id", "")
            self.authorize(actor, project, "query")
            text = body.get("query")
            limit = body.get("limit", 5)
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500 or type(limit) is not int or not 1 <= limit <= 20:
                raise NetworkError("query must be 1-500 characters; limit must be 1-20")
            return search(self.config.get("knowledge_sources", {}), project, text.strip(), limit)
        match = re.fullmatch(r"/v1/tasks/([a-zA-Z0-9_.-]+)(?:/(reconcile|accept|events))?", path)
        if match:
            task_id, action = match.groups()
            if method == "POST" and action == "reconcile":
                return self.reconcile(actor, task_id)
            if method == "POST" and action == "accept":
                return self.accept(actor, task_id, body)
            if method == "GET" and action in (None, "events"):
                task = self.task_for(actor, task_id, "read")
                return {"events": self.store.events(task_id)} if action else public_task(task)
        raise NetworkError("not found", 404)


def make_server(config, address=None):
    hub = Hub(config)
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentBoardNetwork/" + VERSION

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *args):
            pass  # Never log query strings, tokens or document contents.

        def handle_request(self):
            try:
                url = urlsplit(self.path)
                if self.command == "GET" and url.path == "/health":
                    return self.respond(200, {"ok": True, "service": "agent-board-hub", "version": VERSION})
                actor = hub.authenticate(self.headers.get("Authorization", ""))
                body = {}
                if self.command == "POST":
                    if self.headers.get("Transfer-Encoding"):
                        raise NetworkError("chunked requests are not supported")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 16384:
                        raise NetworkError("request body exceeds 16 KiB", 413)
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise NetworkError("request body must be an object")
                result = hub.route(actor, self.command, url.path, parse_qs(url.query), body)
                self.respond(200, result)
            except NetworkError as exc:
                self.respond(exc.status, {"error": str(exc)})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "invalid request"})
            except Exception:
                self.respond(500, {"error": "server operation failed; existing records retained"})

        do_GET = do_POST = handle_request

        def respond(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    listen = config.get("listen", {})
    address = address or (listen.get("host", "127.0.0.1"), listen.get("port", 8940))
    if not loopback(address[0]) and not listen.get("cert_file") and not listen.get("allow_private_http", False):
        raise NetworkError("non-loopback server needs TLS or explicit trusted private-network HTTP")
    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    server.hub = hub
    if listen.get("cert_file"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(listen["cert_file"], listen["key_file"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
