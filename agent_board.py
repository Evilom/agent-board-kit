#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable local bulletin board for coding agents sharing a Git checkout."""

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOL_VERSION = "1.1.0"
DEFAULT_ACTIVE_STALE_HOURS = 4
DEFAULT_BLOCKED_STALE_HOURS = 24
LOCK_WAIT_SECONDS = 30
LOCK_STALE_SECONDS = 120
VALID_STATUSES = ("active", "blocked", "done", "released")
EVENT_TYPE_BY_COMMAND = {
    "start": "agent.started",
    "update": "agent.updated",
    "block": "agent.blocked",
    "done": "agent.done",
    "release": "agent.released",
}

PROJECT_ROOT = ""
STORAGE_MODE = ""
BOARD_DIR = ""
STATE_PATH = ""
STATE_BACKUP_PATH = ""
MESSAGES_PATH = ""
EVENTS_PATH = ""
LOCK_PATH = ""


class BoardError(RuntimeError):
    """Expected board/configuration failure suitable for a concise CLI error."""


class BoardStateError(BoardError):
    """The current or backup board state cannot be trusted."""


def discover_project_root() -> str:
    explicit = os.environ.get("AGENT_BOARD_ROOT", "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    starts = (os.path.dirname(os.path.abspath(__file__)), os.getcwd())
    visited = set()
    for start in starts:
        current = os.path.abspath(start)
        while current not in visited:
            visited.add(current)
            if os.path.exists(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    # Installed layout is <project>/scripts/agent_board.py.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_project_config(project_root: str) -> Dict[str, Any]:
    path = os.path.join(project_root, ".agents", "board", "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardError(f"agent board config is invalid: {path}: {exc}")
    if not isinstance(config, dict):
        raise BoardError(f"agent board config must be a JSON object: {path}")
    return config


def resolve_git_common_dir(project_root: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or "git rev-parse --git-common-dir failed"
        raise BoardError(f"cannot resolve git-common storage: {detail}")
    common_dir = result.stdout.strip()
    if not os.path.isabs(common_dir):
        common_dir = os.path.join(project_root, common_dir)
    return os.path.abspath(common_dir)


def configure_paths() -> None:
    global PROJECT_ROOT, STORAGE_MODE, BOARD_DIR
    global STATE_PATH, STATE_BACKUP_PATH, MESSAGES_PATH, EVENTS_PATH, LOCK_PATH

    PROJECT_ROOT = discover_project_root()
    explicit_dir = os.environ.get("AGENT_BOARD_DIR", "").strip()
    if explicit_dir:
        STORAGE_MODE = "custom"
        BOARD_DIR = os.path.abspath(os.path.expanduser(explicit_dir))
    else:
        config = load_project_config(PROJECT_ROOT)
        STORAGE_MODE = os.environ.get("AGENT_BOARD_STORAGE", "").strip() or config.get("storage", "checkout")
        if STORAGE_MODE not in ("checkout", "git-common"):
            raise BoardError(
                f"unsupported agent board storage '{STORAGE_MODE}'; use checkout or git-common"
            )
        if STORAGE_MODE == "git-common":
            BOARD_DIR = os.path.join(resolve_git_common_dir(PROJECT_ROOT), "agent-board")
        else:
            BOARD_DIR = os.path.join(PROJECT_ROOT, ".agents", "board")

    STATE_PATH = os.path.join(BOARD_DIR, "state.json")
    STATE_BACKUP_PATH = os.path.join(BOARD_DIR, "state.json.bak")
    MESSAGES_PATH = os.path.join(BOARD_DIR, "messages.jsonl")
    EVENTS_PATH = os.path.join(BOARD_DIR, "events.jsonl")
    LOCK_PATH = os.path.join(BOARD_DIR, ".lock")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()


def record_age_hours(record: Dict[str, Any]) -> float:
    updated = record.get("updated_at") or record.get("started_at")
    if not updated:
        return 0
    try:
        return max(0.0, (time.time() - parse_iso(updated)) / 3600)
    except (TypeError, ValueError):
        return 0


def is_open_status(record: Dict[str, Any]) -> bool:
    return record.get("status") in ("active", "blocked")


def stale_limit_hours(
    record: Dict[str, Any], active_stale_hours: float, blocked_stale_hours: float,
) -> float:
    if record.get("status") == "blocked":
        return blocked_stale_hours
    return active_stale_hours


def is_stale_record(
    record: Dict[str, Any],
    active_stale_hours: float = DEFAULT_ACTIVE_STALE_HOURS,
    blocked_stale_hours: float = DEFAULT_BLOCKED_STALE_HOURS,
) -> bool:
    if not is_open_status(record):
        return False
    return record_age_hours(record) > stale_limit_hours(
        record, active_stale_hours, blocked_stale_hours,
    )


def ensure_board_dir() -> None:
    os.makedirs(BOARD_DIR, exist_ok=True)


def configured_agent() -> str:
    for key in ("AGENT_ID", "AGENT_NAME", "CODEX_AGENT_ID", "CLAUDE_AGENT_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def validate_state(state: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(state, dict):
        return ["top level must be a JSON object"]
    if state.get("version") != 1:
        errors.append("version must be 1")
    agents = state.get("agents")
    if not isinstance(agents, dict):
        return errors + ["agents must be a JSON object"]
    updated_at = state.get("updated_at")
    if updated_at is not None:
        try:
            parse_iso(updated_at)
        except (TypeError, ValueError):
            errors.append("updated_at must use YYYY-MM-DD HH:MM:SS")

    for agent_id, record in agents.items():
        prefix = f"agents.{agent_id}"
        if not isinstance(agent_id, str) or not agent_id:
            errors.append("agent keys must be non-empty strings")
            continue
        if not isinstance(record, dict):
            errors.append(f"{prefix} must be a JSON object")
            continue
        if record.get("agent") != agent_id:
            errors.append(f"{prefix}.agent must match its key")
        if record.get("status") not in VALID_STATUSES:
            errors.append(f"{prefix}.status is invalid")
        for field in ("agent", "tool", "task", "branch", "note", "handoff", "stale_reason"):
            value = record.get(field)
            if value is not None and not isinstance(value, str):
                errors.append(f"{prefix}.{field} must be a string")
        for field in ("scope", "files", "blockers"):
            value = record.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                errors.append(f"{prefix}.{field} must be a string array")
        for field in ("started_at", "updated_at", "finished_at"):
            value = record.get(field)
            if field != "finished_at" and not value:
                errors.append(f"{prefix}.{field} is required")
                continue
            if value:
                try:
                    parse_iso(value)
                except (TypeError, ValueError):
                    errors.append(f"{prefix}.{field} must use YYYY-MM-DD HH:MM:SS")
    return errors


def read_state_file(path: str, label: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            state = json.load(stream)
    except json.JSONDecodeError as exc:
        raise BoardStateError(
            f"{label} is invalid JSON at line {exc.lineno}; run 'agent_board.py doctor' "
            "and 'agent_board.py repair --from-backup'"
        )
    except OSError as exc:
        raise BoardStateError(f"cannot read {label}: {exc}")
    errors = validate_state(state)
    if errors:
        raise BoardStateError(f"{label} is invalid: {'; '.join(errors[:5])}")
    state.setdefault("version", 1)
    state.setdefault("agents", {})
    return state


def load_state() -> Dict[str, Any]:
    ensure_board_dir()
    if not os.path.exists(STATE_PATH):
        return {"version": 1, "agents": {}, "updated_at": None}
    return read_state_file(STATE_PATH, "state.json")


def atomic_write_bytes(path: str, content: bytes) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp_path, path)


def atomic_write_json(path: str, value: Dict[str, Any]) -> None:
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def save_state(state: Dict[str, Any]) -> None:
    ensure_board_dir()
    state["updated_at"] = now_iso()
    errors = validate_state(state)
    if errors:
        raise BoardStateError(f"refusing to write invalid state: {'; '.join(errors[:5])}")
    if os.path.exists(STATE_PATH):
        read_state_file(STATE_PATH, "state.json")
        with open(STATE_PATH, "rb") as stream:
            atomic_write_bytes(STATE_BACKUP_PATH, stream.read())
    atomic_write_json(STATE_PATH, state)


def read_jsonl(path: str, limit: int = 20, record_type: str = "") -> List[Dict[str, Any]]:
    if limit <= 0 or not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if record_type and entry.get("type") != record_type:
                continue
            rows.append(entry)
    return rows[-limit:]


def read_messages(limit: int = 20) -> List[Dict[str, Any]]:
    return read_jsonl(MESSAGES_PATH, limit)


def append_message(entry: Dict[str, Any]) -> None:
    ensure_board_dir()
    entry.setdefault("time", now_iso())
    with open(MESSAGES_PATH, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_events(limit: int = 20, event_type: str = "") -> List[Dict[str, Any]]:
    return read_jsonl(EVENTS_PATH, limit, event_type)


def append_event(entry: Dict[str, Any]) -> None:
    ensure_board_dir()
    event = {"version": 1, "time": now_iso()}
    event.update(entry)
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"warning: event log write failed: {exc}", file=sys.stderr)


def agent_event(
    event_type: str,
    record: Dict[str, Any],
    previous: Dict[str, Any],
    conflicts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    event = {
        "type": event_type,
        "agent": record.get("agent", ""),
        "tool": record.get("tool", ""),
        "status": record.get("status", ""),
        "task": record.get("task", ""),
        "scope": record.get("scope", []),
        "files": record.get("files", []),
        "branch": record.get("branch", ""),
        "note": record.get("note", ""),
        "blockers": record.get("blockers", []),
        "handoff": record.get("handoff", ""),
        "started_at": record.get("started_at", ""),
        "updated_at": record.get("updated_at", ""),
    }
    if previous.get("status"):
        event["previous_status"] = previous.get("status", "")
    if record.get("finished_at"):
        event["finished_at"] = record.get("finished_at")
    if record.get("stale_reason"):
        event["stale_reason"] = record.get("stale_reason")
    if conflicts:
        event["conflicts"] = conflicts
    return event


class BoardLock:
    def __init__(self) -> None:
        self.token = f"{os.getpid()}-{time.time_ns()}"

    def __enter__(self) -> "BoardLock":
        ensure_board_dir()
        deadline = time.time() + LOCK_WAIT_SECONDS
        while True:
            try:
                fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "pid": os.getpid(),
                        "token": self.token,
                        "time": time.time(),
                    }))
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(LOCK_PATH)
                    if age > LOCK_STALE_SECONDS:
                        os.remove(LOCK_PATH)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    raise BoardError(f"agent board lock timed out: {LOCK_PATH}")
                time.sleep(0.1)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as stream:
                owner = json.load(stream)
            if owner.get("token") == self.token:
                os.remove(LOCK_PATH)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass


def split_csv(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def normalize_claim(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = posixpath.normpath(normalized).rstrip("/")
    if normalized == ".":
        return ""
    return normalized.casefold() if os.name == "nt" else normalized


def path_is_within(path: str, scope: str) -> bool:
    return bool(path and scope) and (path == scope or path.startswith(scope + "/"))


def claim_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    left_files = {normalize_claim(value) for value in left.get("files", []) if normalize_claim(value)}
    right_files = {normalize_claim(value) for value in right.get("files", []) if normalize_claim(value)}
    left_scopes = {normalize_claim(value) for value in left.get("scope", []) if normalize_claim(value)}
    right_scopes = {normalize_claim(value) for value in right.get("scope", []) if normalize_claim(value)}

    files = sorted(left_files & right_files)
    scopes = sorted({
        f"{left_scope} <> {right_scope}"
        for left_scope in left_scopes
        for right_scope in right_scopes
        if path_is_within(left_scope, right_scope) or path_is_within(right_scope, left_scope)
    })
    paths_in_scope = sorted({
        f"{path} in {scope}"
        for path in left_files
        for scope in right_scopes
        if path_is_within(path, scope)
    } | {
        f"{path} in {scope}"
        for path in right_files
        for scope in left_scopes
        if path_is_within(path, scope)
    })
    if not files and not scopes and not paths_in_scope:
        return None
    return {
        "agent": left.get("agent", ""),
        "other_agent": right.get("agent", ""),
        "task": left.get("task", ""),
        "other_task": right.get("task", ""),
        "files": files,
        "scopes": scopes,
        "paths_in_scope": paths_in_scope,
    }


def open_records(
    state: Dict[str, Any],
    active_stale_hours: float = DEFAULT_ACTIVE_STALE_HOURS,
    blocked_stale_hours: float = DEFAULT_BLOCKED_STALE_HOURS,
) -> List[Dict[str, Any]]:
    return [
        record for record in state.get("agents", {}).values()
        if is_open_status(record)
        and not is_stale_record(record, active_stale_hours, blocked_stale_hours)
    ]


def candidate_conflicts(state: Dict[str, Any], candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    for record in open_records(state):
        if record.get("agent") == candidate.get("agent"):
            continue
        overlap = claim_overlap(candidate, record)
        if overlap:
            conflicts.append(overlap)
    return conflicts


def all_conflicts(
    state: Dict[str, Any],
    active_stale_hours: float = DEFAULT_ACTIVE_STALE_HOURS,
    blocked_stale_hours: float = DEFAULT_BLOCKED_STALE_HOURS,
) -> List[Dict[str, Any]]:
    records = open_records(state, active_stale_hours, blocked_stale_hours)
    conflicts: List[Dict[str, Any]] = []
    for index, left in enumerate(records):
        for right in records[index + 1:]:
            overlap = claim_overlap(left, right)
            if overlap:
                conflicts.append(overlap)
    return conflicts


def warn_claim_conflicts(conflicts: List[Dict[str, Any]]) -> None:
    for conflict in conflicts:
        details = conflict.get("files", []) + conflict.get("scopes", []) + conflict.get("paths_in_scope", [])
        detail = "; ".join(details[:4]) or "overlapping claim"
        print(
            f"warning: claim conflict with {conflict.get('other_agent')}: {detail}",
            file=sys.stderr,
        )


def new_agent_record(args: argparse.Namespace) -> Dict[str, Any]:
    timestamp = now_iso()
    return {
        "agent": args.agent,
        "tool": args.tool or "",
        "status": "active",
        "task": args.task,
        "scope": split_csv(args.scope),
        "files": split_csv(args.files),
        "branch": args.branch or "",
        "note": args.note or "",
        "blockers": split_csv(args.blocker),
        "handoff": args.handoff or "",
        "started_at": timestamp,
        "updated_at": timestamp,
    }


def update_list_field(
    record: Dict[str, Any], args: argparse.Namespace, field: str, argument: str, clear_flag: str,
) -> None:
    values = getattr(args, argument, [])
    if values:
        record[field] = split_csv(values)
    elif getattr(args, clear_flag, False):
        record[field] = []


def update_text_field(
    record: Dict[str, Any], args: argparse.Namespace, field: str, argument: str, clear_flag: str = "",
) -> None:
    value = getattr(args, argument, None)
    if value is not None:
        record[field] = value
    elif clear_flag and getattr(args, clear_flag, False):
        record[field] = ""


def cmd_start(args: argparse.Namespace) -> None:
    conflicts: List[Dict[str, Any]] = []
    with BoardLock():
        state = load_state()
        agents = state.setdefault("agents", {})
        previous = agents.get(args.agent, {})
        if previous and is_open_status(previous) and not args.replace:
            raise BoardError(
                f"agent '{args.agent}' already has an open task; use update or start --replace"
            )
        record = new_agent_record(args)
        conflicts = candidate_conflicts(state, record)
        agents[args.agent] = record
        save_state(state)
        event = agent_event("agent.started", record, previous, conflicts)
        if previous and is_open_status(previous):
            event["replaced"] = True
        append_event(event)
    print(f"active: {args.agent}")
    warn_claim_conflicts(conflicts)


def apply_transition_fields(
    record: Dict[str, Any], previous: Dict[str, Any], args: argparse.Namespace, command: str,
) -> None:
    update_text_field(record, args, "tool", "tool")
    update_text_field(record, args, "task", "task")
    update_text_field(record, args, "branch", "branch")
    update_text_field(record, args, "note", "note", "clear_note")
    update_text_field(record, args, "handoff", "handoff", "clear_handoff")
    update_list_field(record, args, "scope", "scope", "clear_scope")
    update_list_field(record, args, "files", "files", "clear_files")
    update_list_field(record, args, "blockers", "blocker", "clear_blockers")
    if command == "update" and previous.get("status") == "blocked" and not args.blocker:
        record["blockers"] = []


def cmd_transition(args: argparse.Namespace, status: str, command: str) -> None:
    conflicts: List[Dict[str, Any]] = []
    with BoardLock():
        state = load_state()
        agents = state.setdefault("agents", {})
        previous = agents.get(args.agent)
        if not previous:
            raise BoardError(f"agent '{args.agent}' has no board record; run start first")
        if previous.get("status") in ("done", "released"):
            if previous.get("status") == status and command in ("done", "release"):
                print(f"{status}: {args.agent} (already closed)")
                return
            raise BoardError(f"agent '{args.agent}' is closed; run start for a new task")

        record = dict(previous)
        record["scope"] = list(previous.get("scope", []))
        record["files"] = list(previous.get("files", []))
        record["blockers"] = list(previous.get("blockers", []))
        apply_transition_fields(record, previous, args, command)
        record["status"] = status
        record["updated_at"] = now_iso()
        record.pop("finished_at", None)
        if command == "block" and not record.get("blockers"):
            raise BoardError("block requires --blocker or an existing blocker")
        if status in ("done", "released"):
            record["finished_at"] = now_iso()
        else:
            conflicts = candidate_conflicts(state, record)
        agents[args.agent] = record
        save_state(state)
        append_event(agent_event(EVENT_TYPE_BY_COMMAND[command], record, previous, conflicts))
    print(f"{status}: {args.agent}")
    warn_claim_conflicts(conflicts)


def status_records(
    state: Dict[str, Any], args: argparse.Namespace,
) -> Tuple[List[Dict[str, Any]], int, int]:
    active = [
        record for record in state.get("agents", {}).values()
        if is_open_status(record)
        and not is_stale_record(record, args.stale_hours, args.blocked_stale_hours)
    ]
    stale = [
        record for record in state.get("agents", {}).values()
        if is_stale_record(record, args.stale_hours, args.blocked_stale_hours)
    ]
    rows: List[Dict[str, Any]] = []
    for record in sorted(
        state.get("agents", {}).values(), key=lambda row: row.get("updated_at", ""), reverse=True,
    ):
        stale_record = is_stale_record(record, args.stale_hours, args.blocked_stale_hours)
        if args.active and (not is_open_status(record) or (stale_record and not args.include_stale)):
            continue
        row = dict(record)
        row["computed_status"] = "stale" if stale_record else record.get("status", "unknown")
        if stale_record:
            row["stale_age_hours"] = round(record_age_hours(record), 2)
        rows.append(row)
    return rows, len(active), len(stale)


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    rows, active_count, stale_count = status_records(state, args)
    messages = read_messages(args.messages)
    if args.json:
        print(json.dumps({
            "version": state.get("version", 1),
            "tool_version": TOOL_VERSION,
            "project_root": PROJECT_ROOT,
            "storage": STORAGE_MODE,
            "updated_at": state.get("updated_at"),
            "active_count": active_count,
            "stale_count": stale_count,
            "total_count": len(state.get("agents", {})),
            "agents": rows,
            "messages": messages,
        }, ensure_ascii=False, indent=2))
        return

    agents = state.get("agents", {})
    if not agents:
        print("agent board: empty")
    else:
        print(
            f"agent board: active={active_count} stale={stale_count} total={len(agents)} "
            f"updated={state.get('updated_at') or '-'} storage={STORAGE_MODE}"
        )
        if stale_count:
            print(
                f"stale active={args.stale_hours:g}h blocked={args.blocked_stale_hours:g}h; "
                "release stale: python scripts/agent_board.py sweep"
            )
        for record in rows:
            print(
                f"{record.get('computed_status')} {record.get('agent', '?')} "
                f"tool={record.get('tool') or '?'} updated={record.get('updated_at') or '-'}"
            )
            print(f"  task {record.get('task') or '-'}")
            print(f"  scope {', '.join(record.get('scope') or []) or '-'}")
            print(f"  files {', '.join(record.get('files') or []) or '-'}")
            if args.verbose and record.get("note"):
                print(f"  note {record.get('note')}")
            if record.get("blockers"):
                print(f"  blockers {', '.join(record.get('blockers') or [])}")
            if args.verbose and record.get("handoff"):
                print(f"  handoff {record.get('handoff')}")
            if record.get("computed_status") == "stale":
                print(f"  stale_age {record_age_hours(record):.1f}h")

    if messages:
        print(f"messages recent={len(messages)}")
        for message in messages:
            recipient = message.get("to") or "all"
            print(
                f"{message.get('time')} {message.get('from', '?')} -> {recipient}: "
                f"{message.get('text', '')}"
            )


def cmd_message(args: argparse.Namespace) -> None:
    with BoardLock():
        message = {"from": args.agent, "to": args.to, "text": args.text}
        append_message(message)
        append_event({
            "type": "message.sent",
            "agent": args.agent,
            "to": args.to,
            "text": args.text,
            "message_time": message.get("time", ""),
        })
    print(f"message: {args.agent} -> {args.to}")


def cmd_events(args: argparse.Namespace) -> None:
    events = read_events(args.limit, args.type)
    if args.json:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        return
    if not events:
        print("events: empty")
        return
    print(f"events recent={len(events)}")
    for event in events:
        agent = event.get("agent") or "-"
        task = event.get("task") or event.get("text") or "-"
        print(f"{event.get('time', '-')} {event.get('type', '?')} agent={agent} task={task}")


def cmd_conflicts(args: argparse.Namespace) -> None:
    state = load_state()
    conflicts = all_conflicts(state, args.stale_hours, args.blocked_stale_hours)
    if args.json:
        print(json.dumps({"conflicts": conflicts}, ensure_ascii=False, indent=2))
        return
    if not conflicts:
        print("claim conflicts: none")
        return
    print(f"claim conflicts: {len(conflicts)}")
    for conflict in conflicts:
        print(
            f"{conflict.get('agent')} <-> {conflict.get('other_agent')}: "
            f"{'; '.join(conflict.get('files', []) + conflict.get('scopes', []) + conflict.get('paths_in_scope', []))}"
        )


def cmd_prune(args: argparse.Namespace) -> None:
    cutoff = time.time() - args.hours * 3600
    removed = 0
    with BoardLock():
        state = load_state()
        agents = state.get("agents", {})
        for agent, record in list(agents.items()):
            if record.get("status") not in ("done", "released"):
                continue
            finished = record.get("finished_at") or record.get("updated_at")
            try:
                finished_ts = parse_iso(finished)
            except (TypeError, ValueError):
                continue
            if finished_ts < cutoff:
                del agents[agent]
                removed += 1
        save_state(state)
        if removed:
            append_event({"type": "board.pruned", "removed": removed, "hours": args.hours})
    print(f"pruned: {removed}")


def release_stale_records(
    state: Dict[str, Any], active_stale_hours: float, blocked_stale_hours: float,
) -> int:
    released = 0
    for record in state.get("agents", {}).values():
        if not is_stale_record(record, active_stale_hours, blocked_stale_hours):
            continue
        previous = dict(record)
        limit = stale_limit_hours(record, active_stale_hours, blocked_stale_hours)
        record["status"] = "released"
        record["finished_at"] = now_iso()
        record["updated_at"] = now_iso()
        record["stale_released_at"] = now_iso()
        record["stale_reason"] = f"auto released after {limit:g}h without update"
        if not record.get("handoff"):
            record["handoff"] = "陈旧公告板记录自动释放；如任务仍在进行，请重新 start。"
        append_event(agent_event("agent.released", record, previous))
        released += 1
    return released


def cmd_sweep(args: argparse.Namespace) -> None:
    with BoardLock():
        state = load_state()
        released = release_stale_records(state, args.stale_hours, args.blocked_stale_hours)
        save_state(state)
    print(f"swept: {released}")


def compact_messages(keep: int) -> int:
    if keep < 0 or not os.path.exists(MESSAGES_PATH):
        return 0
    messages = read_messages(keep)
    try:
        with open(MESSAGES_PATH, "r", encoding="utf-8") as stream:
            before = sum(1 for line in stream if line.strip())
    except OSError:
        before = len(messages)
    content = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in messages)
    atomic_write_bytes(MESSAGES_PATH, content.encode("utf-8"))
    return max(0, before - len(messages))


def cmd_compact(args: argparse.Namespace) -> None:
    pruned = 0
    cutoff = time.time() - args.done_hours * 3600
    with BoardLock():
        state = load_state()
        released = release_stale_records(state, args.stale_hours, args.blocked_stale_hours)
        agents = state.get("agents", {})
        for agent, record in list(agents.items()):
            if record.get("status") not in ("done", "released"):
                continue
            finished = record.get("finished_at") or record.get("updated_at")
            try:
                finished_ts = parse_iso(finished)
            except (TypeError, ValueError):
                continue
            if finished_ts < cutoff:
                del agents[agent]
                pruned += 1
        save_state(state)
        messages_removed = compact_messages(args.keep_messages)
        if pruned or messages_removed:
            append_event({
                "type": "board.compacted",
                "released": released,
                "pruned": pruned,
                "messages_removed": messages_removed,
                "stale_hours": args.stale_hours,
                "blocked_stale_hours": args.blocked_stale_hours,
                "done_hours": args.done_hours,
                "keep_messages": args.keep_messages,
            })
    print(f"compacted: released={released} pruned={pruned} messages_removed={messages_removed}")


def inspect_jsonl(path: str) -> Tuple[int, int]:
    total = 0
    invalid = 0
    if not os.path.exists(path):
        return total, invalid
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                total += 1
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        invalid += 1
                except json.JSONDecodeError:
                    invalid += 1
    except OSError:
        invalid += 1
    return total, invalid


def doctor_report() -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    state: Optional[Dict[str, Any]] = None
    state_summary: Dict[str, Any] = {"exists": os.path.exists(STATE_PATH)}
    if os.path.exists(STATE_PATH):
        try:
            state = read_state_file(STATE_PATH, "state.json")
            state_summary.update({"valid": True, "agents": len(state.get("agents", {}))})
        except BoardStateError as exc:
            state_summary["valid"] = False
            errors.append(str(exc))
    else:
        state_summary.update({"valid": True, "agents": 0})

    backup_summary: Dict[str, Any] = {"exists": os.path.exists(STATE_BACKUP_PATH)}
    if os.path.exists(STATE_BACKUP_PATH):
        try:
            backup = read_state_file(STATE_BACKUP_PATH, "state.json.bak")
            backup_summary.update({"valid": True, "agents": len(backup.get("agents", {}))})
        except BoardStateError as exc:
            backup_summary["valid"] = False
            warnings.append(str(exc))

    lock_summary: Dict[str, Any] = {"exists": os.path.exists(LOCK_PATH)}
    if os.path.exists(LOCK_PATH):
        try:
            age = max(0.0, time.time() - os.path.getmtime(LOCK_PATH))
            lock_summary["age_seconds"] = round(age, 2)
            warnings.append(f"board lock exists ({age:.1f}s old)")
        except OSError as exc:
            warnings.append(f"cannot inspect board lock: {exc}")

    logs: Dict[str, Any] = {}
    for name, path in (("messages", MESSAGES_PATH), ("events", EVENTS_PATH)):
        total, invalid = inspect_jsonl(path)
        logs[name] = {"records": total, "invalid_records": invalid}
        if invalid:
            warnings.append(f"{name}.jsonl contains {invalid} invalid record(s)")

    conflict_count = len(all_conflicts(state)) if state else 0
    if conflict_count:
        warnings.append(f"{conflict_count} active claim conflict(s)")
    status = "error" if errors else ("warning" if warnings else "ok")
    return {
        "status": status,
        "tool_version": TOOL_VERSION,
        "project_root": PROJECT_ROOT,
        "storage": STORAGE_MODE,
        "board_dir": BOARD_DIR,
        "state": state_summary,
        "backup": backup_summary,
        "lock": lock_summary,
        "logs": logs,
        "conflicts": conflict_count,
        "errors": errors,
        "warnings": warnings,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"agent-board doctor: {report['status']}")
        print(f"  version {report['tool_version']}")
        print(f"  storage {report['storage']} -> {report['board_dir']}")
        print(
            f"  state valid={report['state'].get('valid')} agents={report['state'].get('agents', 0)} "
            f"backup={report['backup'].get('valid', '-') if report['backup'].get('exists') else 'missing'}"
        )
        for error in report["errors"]:
            print(f"  error {error}")
        for warning in report["warnings"]:
            print(f"  warning {warning}")
    return 1 if report["errors"] else 0


def cmd_repair(args: argparse.Namespace) -> None:
    if not args.from_backup:
        raise BoardError("repair requires --from-backup")
    with BoardLock():
        if not os.path.exists(STATE_BACKUP_PATH):
            raise BoardStateError("state.json.bak is missing; automatic repair is unavailable")
        read_state_file(STATE_BACKUP_PATH, "state.json.bak")
        if os.path.exists(STATE_PATH) and not args.force:
            try:
                read_state_file(STATE_PATH, "state.json")
            except BoardStateError:
                pass
            else:
                raise BoardError("state.json is valid; use repair --from-backup --force to replace it")

        corrupt_copy = ""
        if os.path.exists(STATE_PATH):
            suffix = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
            corrupt_copy = os.path.join(BOARD_DIR, f"state.json.corrupt-{suffix}")
            shutil.copyfile(STATE_PATH, corrupt_copy)
        with open(STATE_BACKUP_PATH, "rb") as stream:
            atomic_write_bytes(STATE_PATH, stream.read())
        append_event({
            "type": "board.repaired",
            "source": os.path.basename(STATE_BACKUP_PATH),
            "corrupt_copy": os.path.basename(corrupt_copy) if corrupt_copy else "",
        })
    print(f"repaired: {STATE_PATH}")
    if corrupt_copy:
        print(f"  preserved: {corrupt_copy}")


def add_agent_argument(parser: argparse.ArgumentParser, help_text: str) -> None:
    default = configured_agent()
    parser.add_argument(
        "--agent",
        default=default or None,
        required=not bool(default),
        help=help_text + "; alternatively set AGENT_ID",
    )


def add_start_fields(parser: argparse.ArgumentParser) -> None:
    add_agent_argument(parser, "Stable agent id, e.g. codex-map-editor")
    parser.add_argument("--tool", default="", help="Tool name: codex, claude-code, cursor, etc.")
    parser.add_argument("--task", required=True, help="Current task summary")
    parser.add_argument("--scope", action="append", default=[], help="Directory/domain scope, comma-separated OK")
    parser.add_argument("--files", action="append", default=[], help="Touched or planned files, comma-separated OK")
    parser.add_argument("--branch", default="", help="Branch or worktree name")
    parser.add_argument("--note", default="", help="Short progress note")
    parser.add_argument("--blocker", action="append", default=[], help="Initial blocker, comma-separated OK")
    parser.add_argument("--handoff", default="", help="Handoff summary")
    parser.add_argument("--replace", action="store_true", help="Replace this agent's existing open task")


def add_transition_fields(parser: argparse.ArgumentParser) -> None:
    add_agent_argument(parser, "Existing stable agent id")
    parser.add_argument("--tool", default=None, help="Replace tool name")
    parser.add_argument("--task", default=None, help="Replace task summary")

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--scope", action="append", default=[], help="Replace scope, comma-separated OK")
    scope.add_argument("--clear-scope", action="store_true", help="Clear scope")
    files = parser.add_mutually_exclusive_group()
    files.add_argument("--files", action="append", default=[], help="Replace touched files, comma-separated OK")
    files.add_argument("--clear-files", action="store_true", help="Clear touched files")
    blockers = parser.add_mutually_exclusive_group()
    blockers.add_argument("--blocker", action="append", default=[], help="Replace blockers, comma-separated OK")
    blockers.add_argument("--clear-blockers", action="store_true", help="Clear blockers")

    parser.add_argument("--branch", default=None, help="Replace branch or worktree name")
    note = parser.add_mutually_exclusive_group()
    note.add_argument("--note", default=None, help="Replace progress note")
    note.add_argument("--clear-note", action="store_true", help="Clear progress note")
    handoff = parser.add_mutually_exclusive_group()
    handoff.add_argument("--handoff", default=None, help="Replace handoff summary")
    handoff.add_argument("--clear-handoff", action="store_true", help="Clear handoff summary")


def add_stale_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stale-hours", type=float, default=DEFAULT_ACTIVE_STALE_HOURS,
        help="Active records older than this are stale",
    )
    parser.add_argument(
        "--blocked-stale-hours", type=float, default=DEFAULT_BLOCKED_STALE_HOURS,
        help="Blocked records older than this are stale",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared agent bulletin board")
    parser.add_argument("--version", action="version", version=f"agent-board-kit {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show current board")
    status.add_argument("--active", action="store_true", help="Show only active/blocked agents")
    status.add_argument("--messages", type=int, default=0, help="Recent messages to include")
    add_stale_fields(status)
    status.add_argument("--include-stale", action="store_true", help="Show stale open records with --active")
    status.add_argument("--verbose", action="store_true", help="Show notes and handoff details")
    status.add_argument("--json", action="store_true", help="Print one structured JSON object")
    status.set_defaults(func=cmd_status)

    start = sub.add_parser("start", help="Create a fresh active task")
    add_start_fields(start)
    start.set_defaults(func=cmd_start)

    for command, status_name in (
        ("update", "active"), ("block", "blocked"), ("done", "done"), ("release", "released"),
    ):
        transition = sub.add_parser(command, help=f"Set an existing task status to {status_name}")
        add_transition_fields(transition)
        transition.set_defaults(
            func=lambda args, status_value=status_name, command_name=command:
            cmd_transition(args, status_value, command_name)
        )

    message = sub.add_parser("message", help="Append a message for other agents")
    add_agent_argument(message, "Sender agent id")
    message.add_argument("--to", default="all", help="Recipient agent id or all")
    message.add_argument("text", help="Message text")
    message.set_defaults(func=cmd_message)

    events = sub.add_parser("events", help="Show recent typed board events")
    events.add_argument("--limit", type=int, default=20, help="Recent events to show")
    events.add_argument("--type", default="", help="Filter by event type, e.g. agent.started")
    events.add_argument("--json", action="store_true", help="Print raw JSONL events")
    events.set_defaults(func=cmd_events)

    conflicts = sub.add_parser("conflicts", help="Show overlapping active file/scope claims")
    add_stale_fields(conflicts)
    conflicts.add_argument("--json", action="store_true", help="Print structured JSON")
    conflicts.set_defaults(func=cmd_conflicts)

    doctor = sub.add_parser("doctor", help="Validate state, backup, logs, lock, and active claims")
    doctor.add_argument("--json", action="store_true", help="Print structured JSON")
    doctor.set_defaults(func=cmd_doctor)

    repair = sub.add_parser("repair", help="Restore state from the last valid backup")
    repair.add_argument("--from-backup", action="store_true", help="Restore state.json.bak")
    repair.add_argument("--force", action="store_true", help="Replace a currently valid state")
    repair.set_defaults(func=cmd_repair)

    prune = sub.add_parser("prune", help="Remove old done/released records")
    prune.add_argument("--hours", type=int, default=72)
    prune.set_defaults(func=cmd_prune)

    sweep = sub.add_parser("sweep", help="Release stale active/blocked records")
    add_stale_fields(sweep)
    sweep.set_defaults(func=cmd_sweep)

    compact = sub.add_parser("compact", help="Release stale records, prune closed records, and trim messages")
    add_stale_fields(compact)
    compact.add_argument("--done-hours", type=int, default=72)
    compact.add_argument("--keep-messages", type=int, default=20)
    compact.set_defaults(func=cmd_compact)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        configure_paths()
        result = args.func(args)
        return int(result or 0)
    except BoardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
