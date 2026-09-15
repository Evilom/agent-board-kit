"""Portable Agent Board: project-scoped agents, messages and durable work handoffs."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .common import NetworkError, digest, encoded, identifier, now


def text(value, field, maximum=8000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise NetworkError("%s 需要有效文本（最多 %d 字符）" % (field, maximum))
    return value.strip()


def strings(value, field, maximum=30):
    if not isinstance(value, list) or len(value) > maximum:
        raise NetworkError(field + " 必须是列表")
    return [text(v, field, 2000) for v in value]


def scopes(value):
    paths = strings(value, "范围")
    if not paths:
        raise NetworkError("请明确任务范围，可用 . 表示当前工作区")
    for path in paths:
        if "\\" in path or PurePosixPath(path).is_absolute() or ".." in path.split("/") or ":" in path:
            raise NetworkError("任务范围只能使用工作区内相对路径")
    return paths


def fresh(stamp, seconds=75):
    try:
        return 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() < seconds
    except (TypeError, ValueError):
        return False


class Collaboration:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collab_records(
                    kind TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS collab_project ON collab_records(kind,project);
                CREATE TABLE IF NOT EXISTS collab_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                    subject TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collab_requests(
                    project TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(project,actor,request_key));
            """)

    @staticmethod
    def get(db, kind, key):
        row = db.execute("SELECT data FROM collab_records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row:
            raise NetworkError("记录不存在", 404)
        return json.loads(row[0])

    @staticmethod
    def put(db, kind, item):
        db.execute("INSERT INTO collab_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                   (kind, item["id"], item.get("project_id", ""), encoded(item).decode()))

    @staticmethod
    def rows(db, kind, project=None):
        query = "SELECT data FROM collab_records WHERE kind=?"
        values = [kind]
        if project is not None:
            query += " AND project=?"
            values.append(project)
        return [json.loads(row[0]) for row in db.execute(query + " ORDER BY rowid DESC LIMIT 500", values)]

    def event(self, db, actor, item, event, details=None):
        data = {"time": now(), "actor": actor[0], "type": event, "details": details or {}}
        db.execute("INSERT INTO collab_events(project,subject,data) VALUES(?,?,?)",
                   (item["project_id"], item["id"], encoded(data).decode()))

    def atomic_request(self, actor, body, operation, create):
        project = identifier(body.get("project_id"))
        key = identifier(body.get("request_id"))
        fingerprint = digest({"operation": operation, "body": body})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT request_hash,result FROM collab_requests WHERE project=? AND actor=? AND request_key=?",
                                  (project, actor[0], key)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise NetworkError("同一请求编号不能用于不同内容", 409)
                return json.loads(previous[1])
            result = create(db)
            db.execute("INSERT INTO collab_requests VALUES(?,?,?,?,?)",
                       (project, actor[0], key, fingerprint, encoded(result).decode()))
            return result

    def own_agent(self, db, actor, agent_id, session_id=None, online=False):
        agent = self.get(db, "agent", identifier(agent_id))
        self.hub.authorize(actor, agent["project_id"], "collaborate")
        if agent["device_id"] != actor[1]["device_id"] or agent["principal_id"] != actor[0]:
            raise NetworkError("只能操作本设备的 Agent", 403)
        if session_id is not None and agent["session_id"] != session_id:
            raise NetworkError("Agent 会话已被更新，请重新连接", 409)
        if online and (agent["state"] == "offline" or not fresh(agent["last_seen"])):
            raise NetworkError("Agent 已离线，请先恢复连接", 409)
        return agent

    def device_heartbeat(self, actor, body):
        device_id = actor[1]["device_id"]
        item = {"id": device_id, "name": text(body.get("name", device_id), "设备名称", 100),
                "os": text(body.get("os", "unknown"), "系统", 30),
                "environment_id": identifier(body.get("environment_id")),
                "client_version": text(body.get("client_version", "unknown"), "客户端版本", 30),
                "tools": strings(body.get("tools", []), "本机工具"), "last_seen": now()}
        if body.get("runner"):
            item["runner"] = {key: text(body["runner"].get(key), "客户端运行路径", 2000) for key in ("python", "package_root", "config_path")}
        with self.store.connect() as db:
            self.put(db, "device", item)
        return item

    def devices(self, actor):
        allowed = set()
        for project, grant in actor[1].get("grants", {}).items():
            if "read" in grant.get("operations", []):
                allowed.update(ws["device_id"] for ws in self.hub.config["projects"][project].get("workspaces", {}).values())
        with self.store.connect() as db:
            observed = {d["id"]: d for d in self.rows(db, "device")}
        return {"devices": [dict(observed.get(d, {"id": d, "name": d, "tools": [], "last_seen": None}),
                                 online=fresh(observed.get(d, {}).get("last_seen"))) for d in sorted(allowed)]}

    def register_agent(self, actor, body):
        project = identifier(body.get("project_id"))
        workspace = identifier(body.get("workspace_id"))
        self.hub.authorize(actor, project, "collaborate", workspace)
        ws = self.hub.config["projects"][project]["workspaces"][workspace]
        if ws["device_id"] != actor[1]["device_id"]:
            raise NetworkError("Agent 必须在已授权的本机工作区注册", 403)
        local_id = identifier(body.get("agent_id"))
        session = identifier(body.get("session_id"))
        key = "agent-" + digest([actor[0], project, local_id])[:32]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(db, "agent", key)
                if old["session_id"] != session and old["state"] != "offline" and fresh(old["last_seen"]):
                    raise NetworkError("同名 Agent 的会话仍在线，请使用独立名称", 409)
                if old["session_id"] != session and any(w.get("owner", {}).get("session_id") == old["session_id"] for w in self.rows(db, "work", project) if w.get("owner") and w["status"] in ("active", "blocked")):
                    raise NetworkError("该 Agent 仍持有未结束任务，请用 --session " + old["session_id"] + " 恢复原会话", 409)
            except NetworkError as exc:
                if exc.status != 404:
                    raise
            agent = {"id": key, "local_id": local_id, "project_id": project, "workspace_id": workspace,
                     "device_id": actor[1]["device_id"], "principal_id": actor[0], "session_id": session,
                     "name": text(body.get("name", local_id), "Agent 名称", 100),
                     "provider": text(body.get("provider", "mcp"), "Agent 类型", 60),
                     "capabilities": strings(body.get("capabilities", ["collaboration"]), "能力"),
                     "state": "idle", "last_seen": now(), "note": "", "registered_at": now()}
            self.put(db, "agent", agent)
            return agent

    def heartbeat_agent(self, actor, key, body):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = self.own_agent(db, actor, key, identifier(body.get("session_id")))
            state = body.get("state", agent["state"])
            if state not in ("idle", "working", "blocked", "offline"):
                raise NetworkError("无效的 Agent 状态")
            agent.update(state=state, last_seen=now(), note=text(body.get("note", agent["note"]), "进度", 1000, True))
            self.put(db, "agent", agent)
            return agent

    def list_agents(self, actor, project):
        self.hub.authorize(actor, project, "read")
        with self.store.connect() as db:
            agents = self.rows(db, "agent", project)
        return {"agents": [dict(a, online=a["state"] != "offline" and fresh(a["last_seen"])) for a in agents]}

    def create_work(self, actor, body):
        project = identifier(body.get("project_id"))
        self.hub.authorize(actor, project, "collaborate")
        allowed = {"project_id", "request_id", "title", "goal", "scope", "constraints", "acceptance", "target_agent_id"}
        if set(body) - allowed:
            raise NetworkError("任务包含未知字段")
        template = {"project_id": project, "title": text(body.get("title"), "任务名称", 200),
                    "goal": text(body.get("goal"), "目标"), "scope": scopes(body.get("scope", ["."])),
                    "constraints": strings(body.get("constraints", []), "约束"),
                    "acceptance": strings(body.get("acceptance", []), "验收标准"),
                    "created_by": actor[0], "target_agent_id": body.get("target_agent_id") or None}
        if not template["acceptance"]:
            raise NetworkError("请至少填写一条验收标准")
        def create(db):
            if template["target_agent_id"]:
                agent = self.get(db, "agent", identifier(template["target_agent_id"]))
                if agent["project_id"] != project:
                    raise NetworkError("目标 Agent 不属于该项目", 403)
            item = dict(template, id="work-" + uuid.uuid4().hex, status="ready", revision=1,
                        created_at=now(), updated_at=now(), owner=None, attempt_id=None, attempts=[],
                        progress="", blocker=None, handoff=None, result=None, review=None, execution_id=None)
            self.put(db, "work", item)
            self.event(db, actor, item, "work.created")
            return item
        return self.atomic_request(actor, body, "work.create", create)

    def list_work(self, actor, project):
        self.hub.authorize(actor, project, "read")
        with self.store.connect() as db:
            work = self.rows(db, "work", project)
        return {"work": work}

    def work_detail(self, actor, key):
        with self.store.connect() as db:
            item = self.get(db, "work", key)
            self.hub.authorize(actor, item["project_id"], "read")
            events = [dict(json.loads(row[1]), seq=row[0]) for row in db.execute(
                "SELECT seq,data FROM collab_events WHERE subject=? ORDER BY seq", (key,))]
            artifacts = [{k: v for k, v in a.items() if k != "content"} for a in self.rows(db, "artifact", item["project_id"]) if a["work_id"] == key]
        return dict(item, events=events, artifacts=artifacts)

    @staticmethod
    def assert_owner(item, agent, body):
        owner = item["owner"] or {}
        if (owner.get("agent_id") != agent["id"] or owner.get("session_id") != agent["session_id"]
                or body.get("attempt_id") != item["attempt_id"]):
            raise NetworkError("当前执行归属已变化，旧执行不能覆盖任务", 409)

    def transition(self, actor, key, action, body):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self.get(db, "work", key)
            project = item["project_id"]
            self.hub.authorize(actor, project, "accept" if action == "accept" else "collaborate")
            fingerprint = digest([actor[0], action, body])
            if item.get("last_transition") == fingerprint:
                return item
            if body.get("revision") != item["revision"]:
                raise NetworkError("任务已更新，请刷新后重试", 409)
            details = {"note": body["note"]} if isinstance(body.get("note"), str) else {}
            if action == "accept":
                if item["status"] != "review" or not item["result"]:
                    raise NetworkError("任务尚未提交完整结果", 409)
                if any(c["status"] != "passed" for c in item["result"]["checks"]):
                    raise NetworkError("还有失败或未验证的验收项", 409)
                if item["result"]["kind"] == "device-execution" and (item.get("execution") or {}).get("status") != "review":
                    raise NetworkError("设备执行没有完整成功回执，请退回检查", 409)
                item.update(status="done", review={"actor": actor[0], "note": text(body.get("note"), "验收意见", 2000), "time": now()})
            elif action == "assess":
                self.hub.authorize(actor, project, "accept")
                if item["status"] != "review" or (item.get("result") or {}).get("kind") != "device-execution":
                    raise NetworkError("只有待验收的设备执行支持人工逐项审核", 409)
                reviewed = self.validate_result(db, item, body)
                reviewed.update(kind="device-execution", reviewed_by=actor[0])
                item["result"] = reviewed
            elif action == "reopen":
                self.hub.authorize(actor, project, "accept")
                if item["status"] != "review":
                    raise NetworkError("只可退回待验收任务", 409)
                item.update(status="blocked" if item["owner"] else "ready", blocker=text(body.get("note"), "退回原因", 2000), result=None)
            elif action == "assign":
                if item["status"] != "ready":
                    raise NetworkError("只可分配尚未开始的任务", 409)
                target = self.get(db, "agent", identifier(body.get("target_agent_id")))
                if target["project_id"] != project:
                    raise NetworkError("目标 Agent 不属于该项目", 403)
                item["target_agent_id"] = target["id"]
            elif action == "cancel":
                self.hub.authorize(actor, project, "accept")
                if item["status"] != "ready":
                    raise NetworkError("执行中的任务需要先由执行方停止并交接，不能直接标记取消", 409)
                item.update(status="cancelled", progress=text(body.get("note"), "取消原因", 2000))
            else:
                agent = self.own_agent(db, actor, identifier(body.get("agent_id")), identifier(body.get("session_id")), True)
                if agent["project_id"] != project:
                    raise NetworkError("Agent 与任务项目不一致", 403)
                if action in ("claim", "handoff-accept"):
                    if action == "claim":
                        if item["status"] != "ready" or item["target_agent_id"] not in (None, agent["id"]):
                            raise NetworkError("任务已被认领或分配给其他 Agent", 409)
                    elif item["status"] != "handoff" or item["handoff"]["to_agent_id"] != agent["id"]:
                        raise NetworkError("没有发给该 Agent 的交接", 409)
                    attempt = {"id": "attempt-" + uuid.uuid4().hex, "agent_id": agent["id"], "session_id": agent["session_id"], "started_at": now()}
                    item["attempts"].append(attempt)
                    item.update(owner={"agent_id": agent["id"], "session_id": agent["session_id"], "device_id": agent["device_id"], "workspace_id": agent["workspace_id"]},
                                attempt_id=attempt["id"], status="active", target_agent_id=agent["id"], blocker=None)
                    if item["handoff"]:
                        item["handoff"]["accepted_at"] = now()
                    agent["state"] = "working"
                else:
                    self.assert_owner(item, agent, body)
                    if item["status"] not in ("active", "blocked"):
                        raise NetworkError("任务当前不能接受该执行的更新", 409)
                    if action == "progress":
                        item.update(status="active", blocker=None, progress=text(body.get("note"), "进度", 4000))
                        agent["state"] = "working"
                    elif action == "block":
                        item.update(status="blocked", blocker=text(body.get("note"), "阻塞原因", 4000))
                        agent["state"] = "blocked"
                    elif action == "handoff":
                        target = self.get(db, "agent", identifier(body.get("to_agent_id")))
                        if target["project_id"] != project or target["id"] == agent["id"]:
                            raise NetworkError("交接对象必须是同项目的另一位 Agent")
                        if body.get("released") is not True:
                            raise NetworkError("交接前必须确认本次执行已停止修改")
                        item.update(status="handoff", handoff={"from_agent_id": agent["id"], "to_agent_id": target["id"],
                            "completed": text(body.get("completed"), "已完成内容", 4000),
                            "remaining": text(body.get("remaining"), "剩余工作", 4000),
                            "context": text(body.get("context", ""), "交接上下文", 8000, True), "created_at": now(), "released": True})
                        details = dict(item["handoff"])
                        agent["state"] = "idle"
                    elif action == "result":
                        item["result"] = self.validate_result(db, item, body)
                        item["attempts"][-1]["result"] = item["result"]
                        details = {"summary": item["result"]["summary"]}
                        item.update(status="review", blocker=None)
                        agent["state"] = "idle"
                    else:
                        raise NetworkError("未知任务操作", 404)
                agent["last_seen"] = now()
                self.put(db, "agent", agent)
            item.update(revision=item["revision"] + 1, updated_at=now(), last_transition=fingerprint)
            self.put(db, "work", item)
            self.event(db, actor, item, "work." + action, details)
            return item

    def validate_result(self, db, item, body):
        summary = text(body.get("summary"), "结果摘要", 8000)
        checks = body.get("checks")
        if not isinstance(checks, list) or len(checks) != len(item["acceptance"]):
            raise NetworkError("结果必须逐条对应验收标准")
        normalized = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict) or check.get("status") not in ("passed", "failed", "unverified"):
                raise NetworkError("每条验收项需要 passed / failed / unverified 状态")
            normalized.append({"criterion": item["acceptance"][index], "status": check["status"], "evidence": text(check.get("evidence"), "验证依据", 4000)})
        references = strings(body.get("artifact_ids", []), "产物编号")
        for key in references:
            artifact = self.get(db, "artifact", key)
            if artifact["work_id"] != item["id"] or artifact["attempt_id"] != item["attempt_id"]:
                raise NetworkError("产物不属于当前执行", 403)
        return {"summary": summary, "checks": normalized, "artifact_ids": references,
                "attempt_id": item["attempt_id"], "submitted_at": now(), "kind": "agent-reported-evidence"}

    def send_message(self, actor, body):
        project = identifier(body.get("project_id"))
        self.hub.authorize(actor, project, "collaborate")
        def send(db):
            target = self.get(db, "agent", identifier(body.get("to_agent_id")))
            if target["project_id"] != project:
                raise NetworkError("消息接收方不属于该项目", 403)
            sender = None
            if body.get("from_agent_id"):
                sender = self.own_agent(db, actor, body["from_agent_id"], identifier(body.get("session_id")))
                if sender["project_id"] != project:
                    raise NetworkError("发送方不属于该项目", 403)
            work_id = body.get("work_id") or None
            if work_id and self.get(db, "work", work_id)["project_id"] != project:
                raise NetworkError("消息线程不属于该项目", 403)
            message = {"id": "msg-" + uuid.uuid4().hex, "project_id": project, "work_id": work_id,
                       "to_agent_id": target["id"], "from_agent_id": sender["id"] if sender else None,
                       "from_principal": actor[0], "body": text(body.get("body"), "消息正文", 8000),
                       "needs_ack": body.get("needs_ack", True) is True, "created_at": now(),
                       "delivered_at": None, "acknowledged_at": None, "reply_to": body.get("reply_to") or None}
            if message["reply_to"] and self.get(db, "message", message["reply_to"])["project_id"] != project:
                raise NetworkError("回复的消息不属于该项目", 403)
            if message["reply_to"]:
                previous = self.get(db, "message", message["reply_to"])
                own = {a["id"] for a in self.rows(db, "agent", project) if a["principal_id"] == actor[0]}
                if not previous["work_id"] and previous["from_principal"] != actor[0] and previous["to_agent_id"] not in own:
                    raise NetworkError("无权回复该私信", 403)
            self.put(db, "message", message)
            return message
        return self.atomic_request(actor, body, "message.send", send)

    def messages(self, actor, project, agent_id=None, session_id=None):
        self.hub.authorize(actor, project, "read")
        with self.store.connect() as db:
            if agent_id:
                db.execute("BEGIN IMMEDIATE")
                agent = self.own_agent(db, actor, agent_id, session_id)
                if agent["project_id"] != project:
                    raise NetworkError("收件箱项目不一致", 403)
                result = [json.loads(row[0]) for row in db.execute(
                    "SELECT data FROM collab_records WHERE kind='message' AND project=? AND json_extract(data,'$.to_agent_id')=? AND json_extract(data,'$.acknowledged_at') IS NULL ORDER BY rowid LIMIT 500", (project, agent_id))]
                for message in result:
                    if not message["delivered_at"]:
                        message["delivered_at"] = now()
                        self.put(db, "message", message)
            else:
                own = {a["id"] for a in self.rows(db, "agent", project) if a["principal_id"] == actor[0]}
                result = [m for m in self.rows(db, "message", project) if m["from_principal"] == actor[0] or m["to_agent_id"] in own or m["work_id"]]
        return {"messages": result}

    def ack_message(self, actor, key, body):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self.get(db, "message", key)
            agent = self.own_agent(db, actor, identifier(body.get("agent_id")), identifier(body.get("session_id")))
            if item["to_agent_id"] != agent["id"]:
                raise NetworkError("只有接收方可以确认消息", 403)
            item["delivered_at"] = item["delivered_at"] or now()
            item["acknowledged_at"] = item["acknowledged_at"] or now()
            self.put(db, "message", item)
            return item

    def upload(self, actor, key, body):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            work = self.get(db, "work", key)
            agent = self.own_agent(db, actor, identifier(body.get("agent_id")), identifier(body.get("session_id")), True)
            self.assert_owner(work, agent, body)
            if work["status"] not in ("active", "blocked"):
                raise NetworkError("该执行已不再接受产物", 409)
            try:
                raw = base64.b64decode(body.get("content", ""), validate=True)
            except (ValueError, TypeError):
                raise NetworkError("产物必须使用有效 base64 编码")
            if not raw or len(raw) > 1024 * 1024:
                raise NetworkError("单个产物大小须为 1 字节至 1 MiB")
            sha = hashlib.sha256(raw).hexdigest()
            if body.get("sha256") != sha:
                raise NetworkError("产物校验失败")
            name = text(body.get("name"), "产物名称", 200)
            if any(c in name for c in ("/", "\\", "\r", "\n")):
                raise NetworkError("产物名称不能包含路径")
            item = {"id": "artifact-" + digest([key, work["attempt_id"], name, sha])[:32], "project_id": work["project_id"],
                    "work_id": key, "attempt_id": work["attempt_id"], "name": name, "sha256": sha, "size": len(raw),
                    "content": base64.b64encode(raw).decode(), "created_at": now()}
            self.put(db, "artifact", item)
            return {k: v for k, v in item.items() if k != "content"}

    def route(self, actor, method, path, query, body):
        import re
        project = (query.get("project_id") or [""])[0]
        if path == "/v1/devices/heartbeat" and method == "POST":
            return self.device_heartbeat(actor, body)
        if path == "/v1/devices" and method == "GET":
            return self.devices(actor)
        if path == "/v1/capabilities" and method == "GET":
            from .execution import CAPABILITIES
            return {"capabilities": [dict(id=k, **v) for k, v in CAPABILITIES.items()]}
        if path == "/v1/agents":
            return self.register_agent(actor, body) if method == "POST" else self.list_agents(actor, project)
        match = re.fullmatch(r"/v1/agents/([\w.-]+)/heartbeat", path)
        if match and method == "POST":
            return self.heartbeat_agent(actor, match[1], body)
        if path == "/v1/work":
            return self.create_work(actor, body) if method == "POST" else self.list_work(actor, project)
        match = re.fullmatch(r"/v1/work/([\w.-]+)(?:/([\w-]+))?", path)
        if match:
            key, action = match.groups()
            if method == "GET" and action is None:
                return self.work_detail(actor, key)
            if method == "POST" and action == "artifacts":
                return self.upload(actor, key, body)
            if method == "POST" and action in ("run", "reconcile"):
                from . import managed
                return managed.run(self, actor, key, body) if action == "run" else managed.reconcile(self, actor, key)
            if method == "POST" and action:
                return self.transition(actor, key, action, body)
        if path == "/v1/messages":
            if method == "POST":
                return self.send_message(actor, body)
            return self.messages(actor, project, (query.get("agent_id") or [None])[0], (query.get("session_id") or [None])[0])
        match = re.fullmatch(r"/v1/messages/([\w.-]+)/ack", path)
        if match and method == "POST":
            return self.ack_message(actor, match[1], body)
        match = re.fullmatch(r"/v1/artifacts/([\w.-]+)", path)
        if match and method == "GET":
            with self.store.connect() as db:
                artifact = self.get(db, "artifact", match[1])
                self.hub.authorize(actor, artifact["project_id"], "read")
                raw = base64.b64decode(artifact["content"], validate=True)
                if len(raw) != artifact["size"] or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                    raise NetworkError("已保存的产物未通过完整性检查，请保留记录与备份", 500)
                return artifact
        raise NetworkError("接口不存在", 404)
