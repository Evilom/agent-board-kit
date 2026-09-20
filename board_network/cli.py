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
                                   "grants": {project: {"operations": ["read", "query", "execute", "accept", "collaborate"], "workspaces": [device]}}}},
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
    validate_url(args.hub_url, getattr(args, "allow_private_http", False))
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
        args.project: {"operations": ["read", "query", "execute", "collaborate"], "workspaces": [workspace]}}}
    # The server's local operator can dispatch to the new, explicitly paired workspace.
    cfg["principals"][cfg["worker"]["device_id"]]["grants"][args.project]["workspaces"].append(workspace)
    Hub(cfg)  # Validate before replacing config; ledger is only opened, never reset.
    client = {"version": 1, "role": "client", "runtime_dir": "runtime", "execution_enabled": False,
              "hub": {"url": args.hub_url, "token_file": token.name, "allow_private_http": getattr(args, "allow_private_http", False)},
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
    p = argparse.ArgumentParser(description="Agent Board：设备、Agent、任务、消息和知识协作")
    p.add_argument("--config", required=True, help="本设备统一配置文件")
    sub = p.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init-server", help="Create a server + local client configuration")
    init.add_argument("--project", required=True)
    init.add_argument("--device", default="mac-local")
    init.add_argument("--root", default=".")
    sub.add_parser("serve", help="Run just the Board HTTP server")
    sub.add_parser("service", help="Run server + local client, or client only, based on role")
    sub.add_parser("runtime-install", help="Download and verify the pinned Dagu binary for this machine")
    sub.add_parser("projects", help="List authorized projects and target workspaces")
    sub.add_parser("doctor", help="检查设备连接、资源目录与本机服务所有权")
    sub.add_parser("open", help="在浏览器打开中文客户端，凭据不写入浏览器存储")
    sub.add_parser("upgrade", help="备份并检查旧版配置与数据库，启用协作权限")
    sub.add_parser("device", help="运行本设备心跳（由 service 自动管理）")
    sub.add_parser("devices", help="查看真实设备连接状态")
    resources = sub.add_parser('resources', help='发现已授权的全部设备工程')
    resources.add_argument('--query', default='')
    add = sub.add_parser('resource-add', help='在本设备统一配置中登记工程；spec 为不含密钥值的 JSON')
    add.add_argument('--key', required=True)
    add.add_argument('--spec', required=True)
    call = sub.add_parser('resource-call', help='在目标设备执行已授权操作并等待真实回执')
    call.add_argument('--resource', required=True)
    call.add_argument('--operation', required=True)
    call.add_argument('--arguments', default='{}', help='JSON 操作参数；内容复杂时使用 --arguments-file')
    call.add_argument('--arguments-file')
    call.add_argument('--request-id', required=True)
    call.add_argument('--wait', type=int, default=30)
    get = sub.add_parser('resource-result', help='查看已有操作结果，不重复执行')
    get.add_argument('operation_id')
    copy = sub.add_parser('resource-copy', help='跨设备分块传输，校验 SHA256；重试沿用 request-id')
    for field in ('source', 'source-path', 'target', 'target-path', 'request-id'):
        copy.add_argument('--' + field, required=True)
    copy.add_argument('--expected-sha256', default='absent')
    copy.add_argument('--wait', type=int, default=60)
    startup = sub.add_parser('service-install', help='安装登录自启的轻量设备服务，不启动模型')
    startup.add_argument('--activate', action='store_true')
    sub.add_parser('ecosystem-mcp', help='设备级 MCP：跨工程工具，独立于聊天会话，无需 project/name/session')
    sub.add_parser('ecosystem-connection', help='输出可持久接入的设备级 MCP 配置，不创建聊天 Agent')
    probe = sub.add_parser('ecosystem-check', help='以本设备身份发现工程、读取上下文并确认结果取回')
    probe.add_argument('--query', required=True, help='唯一工程名称或项目 key')
    probe.add_argument('--command', help='可选：执行该工程明确提供的固定命令')
    probe.add_argument('--request-id', help='断线重试沿用同一编号')
    probe.add_argument('--wait', type=int, default=30)
    for name in ("mcp", "agent", "connection"):
        command = sub.add_parser(name, help={"mcp": "现有 Agent 的 MCP stdio 接口", "agent": "启动并接入本机 Codex / Claude Code", "connection": "输出本机 MCP 接入配置"}[name])
        command.add_argument("--project", required=True)
        command.add_argument("--workspace", required=True)
        command.add_argument("--name", required=True, help="本项目内独立的会话名称")
        command.add_argument("--session", help="恢复原连接的会话编号；只用于本人未结束的任务")
        command.add_argument("--provider", choices=("codex", "claude", "mcp") if name != "agent" else ("codex", "claude"), default="codex")
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
    enroll.add_argument("--allow-private-http", action="store_true", help="明确允许可信私有网络上的 HTTP 连接")
    return p


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.action == "init-server":
            result = init_server(args)
        elif args.action == "pair":
            result = pair(args)
        elif args.action == "upgrade":
            from .upgrade import upgrade
            result = upgrade(args.config)
        else:
            cfg = load_config(args.config)
            if args.action in ('resource-add', 'resource-call', 'resource-copy', 'service-install', 'resource-result', 'resources'):
                from .resource_cli import register, invoke, copy_resource
                if args.action == 'resource-add':
                    result = register(args.config, args.key, json.loads(Path(args.spec).read_text(encoding='utf-8')))
                elif args.action == 'resource-call':
                    data = json.loads(Path(args.arguments_file).read_text(encoding='utf-8')) if args.arguments_file else json.loads(args.arguments)
                    result = invoke(cfg, args.resource, args.operation, data, args.request_id, args.wait)
                elif args.action == 'resource-copy':
                    result = copy_resource(cfg, args.source, args.source_path, args.target, args.target_path, args.expected_sha256, args.request_id, args.wait)
                elif args.action == 'resource-result':
                    from .resource_cli import confirm_retrieval
                    result = confirm_retrieval(cfg['hub'], request_json(cfg['hub'], '/v1/resource-operations/' + identifier(args.operation_id), board_errors=True))
                elif args.action == 'resources':
                    result = request_json(cfg['hub'], '/v1/resources?query=' + quote(args.query), board_errors=True)
                else:
                    from .service_install import install
                    result = install(args.config, args.activate)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.action == 'ecosystem-check':
                from .resource_cli import check_ecosystem
                result = check_ecosystem(cfg, args.query, args.command, args.request_id, args.wait)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result['passed'] else 1
            if args.action == "device":
                from .client import device_loop
                device_loop(cfg, args.config)
                return 0
            if args.action == 'ecosystem-connection':
                result = {'mcpServers': {'agent_ecosystem': {'command': sys.executable,
                    'args': [str(Path(__file__).resolve().parent.parent / 'agent_board.py'), 'network', '--config', str(Path(args.config).resolve()), 'ecosystem-mcp']}}}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.action in ('mcp', 'ecosystem-mcp'):
                from .mcp import serve
                return serve(cfg, args)
            if args.action == "agent":
                from .client import launch
                return launch(cfg, args.config, args)
            if args.action == "connection":
                from .client import connection
                result = {"mcpServers": {"agent_board": connection(args.config, args.project, args.workspace, args.name, args.provider, args.session)}}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.action == "open":
                import webbrowser
                ticket = request_json(cfg["hub"], "/v1/browser-ticket", {})
                url = cfg["hub"]["url"].rstrip("/") + "/#ticket=" + ticket["ticket"]
                webbrowser.open(url)
                print("已打开中文客户端。登录链接仅使用一次，有效期 60 秒。")
                return 0
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
                from .diagnostics import doctor
                result = doctor(cfg, args.config)
            elif args.action == "devices":
                result = request_json(cfg["hub"], "/v1/devices")
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
