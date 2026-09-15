"""Portable Agent Board server/client command line."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

from .common import NetworkError, encoded, identifier, load_config, request_json


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def init_server(args):
    path = Path(args.config).expanduser().resolve()
    if path.exists():
        raise NetworkError("config already exists; refusing to replace it")
    project = identifier(args.project)
    device = identifier(args.device)
    environment = device + "-native"
    root = str(Path(args.root).expanduser().resolve())
    token_path = path.with_name(device + ".token")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with token_path.open("x", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32) + "\n")
    token_path.chmod(0o600)
    workspace = {"root": root, "device_id": device, "environment_id": environment,
                 "os": platform.system(), "python": sys.executable}
    cfg = {"version": 1, "role": "server", "database": "board.sqlite3", "runtime_dir": "runtime",
           "listen": {"host": "127.0.0.1", "port": 8940},
           "dagu": {"url": "http://127.0.0.1:8188"},
           "hub": {"url": "http://127.0.0.1:8940", "token_file": token_path.name},
           "worker": {"device_id": device, "environment_id": environment, "coordinator": "127.0.0.1:51855",
                      "peer_insecure": True},
           "coordinator": {"host": "127.0.0.1", "advertise": "127.0.0.1", "port": 51855},
           "projects": {project: {"name": project, "workspaces": {device: workspace}}},
           "principals": {device: {"device_id": device, "token_file": token_path.name,
                                   "grants": {project: {"operations": ["read", "query", "execute", "accept"], "workspaces": [device]}}}},
           "knowledge_sources": {"project-docs": {"kind": "documents", "root": root, "projects": [project],
                                                    "prefixes": ["README.md", "README.zh-CN.md", "docs"]}}}
    write_new(path, cfg)
    return {"config": str(path), "role": "server", "next": "network runtime-install, then network service"}


def pair(args):
    # Pairing is a local administrator action, never an unauthenticated HTTP endpoint.
    from .hub import Hub
    path = Path(args.config).expanduser().resolve()
    cfg = load_config(path)
    if cfg.get("role") != "server":
        raise NetworkError("pairing requires the server config")
    from .common import validate_url
    validate_url(args.hub_url)
    if args.project not in cfg["projects"]:
        raise NetworkError("project must already be registered")
    device, environment, workspace = map(identifier, (args.device, args.environment, args.workspace))
    if device in cfg["principals"] or workspace in cfg["projects"][args.project]["workspaces"]:
        raise NetworkError("device or workspace already exists; edit its grant explicitly instead", 409)
    export_dir = path.parent / "pairings" / device
    export_dir.mkdir(parents=True, exist_ok=True)
    token = export_dir / (device + ".token")
    with token.open("x", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32) + "\n")
    token.chmod(0o600)
    cfg["projects"][args.project]["workspaces"][workspace] = {
        "device_id": device, "environment_id": environment, "os": args.os,
        "root": args.root, "python": "python" if args.os == "Windows" else "python3"}
    cfg["principals"][device] = {"device_id": device, "token_file": str(token), "grants": {
        args.project: {"operations": ["read", "query", "execute"], "workspaces": [workspace]}}}
    # The server's local operator can dispatch to the new, explicitly paired workspace.
    cfg["principals"][cfg["worker"]["device_id"]]["grants"][args.project]["workspaces"].append(workspace)
    Hub(cfg)  # Validate before replacing config; ledger is only opened, never reset.
    client = {"version": 1, "role": "client", "runtime_dir": "runtime",
              "hub": {"url": args.hub_url, "token_file": token.name},
              "worker": {"device_id": device, "environment_id": environment, "coordinator": args.coordinator,
                         "peer_insecure": False, "peer_ca_file": "certs/ca.pem", "peer_cert_file": "certs/client.pem",
                         "peer_key_file": "certs/client-key.pem"},
              "project_id": args.project, "workspace_id": workspace, "workspace_root": args.root}
    write_new(export_dir / "client.json", client)
    backup = path.with_name(path.name + ".bak-" + secrets.token_hex(4))
    backup.write_bytes(path.read_bytes())
    backup.chmod(0o600)
    if backup.read_bytes() != path.read_bytes():
        raise NetworkError("config changed during backup; pairing was not applied")
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    temp.write_bytes(encoded(cfg) + b"\n")
    temp.chmod(0o600)
    os.replace(temp, path)
    load_config(path)
    return {"client_config": str(export_dir / "client.json"), "token_file": str(token),
            "backup": str(backup), "next": "Transfer pairing files securely, configure TLS certificates, restart server, start client."}


def parser():
    p = argparse.ArgumentParser(description="Agent Board optional server/client adapters")
    p.add_argument("--config", required=True, help="Single local server or client config")
    sub = p.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init-server", help="Create a server + local client configuration")
    init.add_argument("--project", required=True)
    init.add_argument("--device", default="mac-local")
    init.add_argument("--root", default=".")
    sub.add_parser("serve", help="Run just the Board HTTP server")
    sub.add_parser("service", help="Run server + local client, or client only, based on role")
    sub.add_parser("runtime-install", help="Download and verify the pinned Dagu binary for this machine")
    sub.add_parser("projects", help="List authorized projects and target workspaces")
    sub.add_parser("doctor", help="Check config, Hub, and Dagu connectivity")
    q = sub.add_parser("query", help="Query authorized knowledge with source versions")
    q.add_argument("--project", required=True)
    q.add_argument("text")
    tasks = sub.add_parser("tasks")
    tasks.add_argument("--project", required=True)
    ins = sub.add_parser("inspect", help="Dispatch the registered git.inspect/v1 recipe")
    for flag in ("project", "workspace", "commit", "key"):
        ins.add_argument("--" + flag, required=True)
    ins.add_argument("--title", default="Read-only Git workspace inspection")
    for name in ("show", "reconcile", "events", "accept"):
        item = sub.add_parser(name)
        item.add_argument("task_id")
        if name == "accept":
            item.add_argument("--note", required=True)
    enroll = sub.add_parser("pair", help="Locally authorize one device for one project and export its client config")
    for flag in ("project", "device", "environment", "workspace", "root", "hub-url", "coordinator"):
        enroll.add_argument("--" + flag, required=True)
    enroll.add_argument("--os", choices=("Windows", "Darwin", "Linux"), required=True)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.action == "init-server":
            result = init_server(args)
        elif args.action == "pair":
            result = pair(args)
        else:
            cfg = load_config(args.config)
            if args.action == "serve":
                from .hub import make_server
                with make_server(cfg) as server:
                    print("Agent Board server listening on %s:%s" % server.server_address, flush=True)
                    server.serve_forever()
                return 0
            if args.action in ("service", "runtime-install"):
                from .runtime import install_runtime, service
                result = install_runtime(cfg) if args.action == "runtime-install" else service(cfg, args.config)
            elif args.action == "doctor":
                result = {"role": cfg.get("role"), "hub": request_json(cfg["hub"], "/health"),
                          "authorization": request_json(cfg["hub"], "/v1/projects")}
                if cfg.get("role") == "server":
                    from .dagu import Dagu
                    result["workers"] = Dagu(cfg["dagu"]).workers()
            elif args.action == "projects":
                result = request_json(cfg["hub"], "/v1/projects")
            elif args.action == "tasks":
                result = request_json(cfg["hub"], "/v1/tasks?project_id=" + quote(args.project, safe=""))
            elif args.action == "query":
                result = request_json(cfg["hub"], "/v1/knowledge/search", {"project_id": args.project, "query": args.text})
            elif args.action == "inspect":
                result = request_json(cfg["hub"], "/v1/tasks", {"project_id": args.project, "workspace_id": args.workspace,
                    "expected_commit": args.commit, "idempotency_key": args.key, "title": args.title})
            else:
                path = "/v1/tasks/" + identifier(args.task_id)
                if args.action != "show":
                    path += "/" + args.action
                body = {"note": args.note} if args.action == "accept" else {} if args.action == "reconcile" else None
                result = request_json(cfg["hub"], path, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (NetworkError, OSError, ValueError, KeyError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
