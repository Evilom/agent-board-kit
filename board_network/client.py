"""Portable Agent Board: Device heartbeat and per-session connection to installed agent CLIs."""
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import VERSION
from .common import NetworkError, request_json


def agent_executable(config, provider):
    command = config.get('agent_commands', {}).get(provider, provider)
    if not isinstance(command, str):
        raise NetworkError('agent_commands 只能配置单个可执行文件路径')
    return shutil.which(command)


class AgentClient:
    def __init__(self, config, project, workspace, name, provider='mcp', session=None):
        self.config, self.endpoint, self.project = config, config['hub'], project
        self.session = session or 'session-' + uuid.uuid4().hex
        self.registration = dict(project_id=project, workspace_id=workspace, agent_id=name,
                                 name=name, provider=provider, session_id=self.session,
                                 capabilities=['tasks', 'messages', 'handoff', 'knowledge', 'artifacts'])
        if name in config.get('agent_profiles', {}):
            self.registration['profile'] = config['agent_profiles'][name]
        self.agent = None
        self.stopped = threading.Event()
        self.thread = None

    def call(self, path, body=None):
        return request_json(self.endpoint, path, body, board_errors=True)

    def connect(self):
        self.agent = self.call('/v1/agents', self.registration)
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self.agent

    def identity(self):
        return dict(agent_id=self.agent['id'], session_id=self.session)

    def heartbeat(self):
        while not self.stopped.wait(20):
            try:
                self.call('/v1/agents/' + self.agent['id'] + '/heartbeat', {'session_id': self.session})
            except (NetworkError, OSError):
                pass  # Loss of connectivity never grants a new owner or repeats work.

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.agent:
            try:
                self.call('/v1/agents/' + self.agent['id'] + '/heartbeat', dict(session_id=self.session, state='offline'))
            except (NetworkError, OSError):
                pass

    def tools_call(self, name, args):
        body = dict(args)
        if name == 'board_resources':
            return self.call('/v1/resources?' + urlencode({'query': body.get('query', '')}))
        if name == 'board_remote':
            return self.call('/v1/resource-operations', body)
        if name == 'board_operation':
            return self.call('/v1/resource-operations/' + body['operation_id'])
        if name == 'board_cancel_operation':
            return self.call('/v1/resource-operations/' + body['operation_id'] + '/cancel', {})
        if name == 'board_shared_knowledge':
            return self.call('/v1/shared-knowledge/search', body)
        if name == 'board_status':
            agents = self.call('/v1/agents?' + urlencode({'project_id': self.project}))['agents']
            return {'self': next((a for a in agents if a['id'] == self.agent['id']), self.agent),
                    'devices': self.call('/v1/devices')['devices'], 'agents': agents,
                    'notifications': self.tools_call('board_updates', {})}
        if name == 'board_updates':
            return self.call('/v1/agents/' + self.agent['id'] + '/updates?' + urlencode({'session_id': self.session}))
        if name == 'board_profile':
            return self.call('/v1/agents/' + self.agent['id'] + '/profile', dict(body, session_id=self.session))
        if name == 'board_find':
            return self.call('/v1/agents/discover?' + urlencode({'project_id': self.project,
                             'exclude': self.agent['id'], 'skill': body.get('skills', [])}, doseq=True))
        if name == 'board_bulletins':
            return self.call('/v1/bulletins?' + urlencode(dict(project_id=self.project,
                             **self.identity(), before=body.get('before', ''), unread='1' if body.get('unread_only') else '0')))
        if name == 'board_publish':
            return self.call('/v1/bulletins', dict(body, project_id=self.project,
                             from_agent_id=self.agent['id'], session_id=self.session))
        if name == 'board_read':
            return self.call('/v1/bulletins/' + body['bulletin_id'] + '/read', self.identity())
        if name == 'board_tasks':
            return self.call('/v1/work?' + urlencode({'project_id': self.project}))
        if name == 'board_task':
            item = self.call('/v1/work/' + body['work_id'])
            if item['project_id'] != self.project:
                raise NetworkError('该任务不属于当前 Agent 项目', 403)
            return item
        if name == 'board_create':
            body['project_id'] = self.project
            return self.call('/v1/work', body)
        if name == 'board_inbox':
            return self.call('/v1/messages?' + urlencode(dict(project_id=self.project, **self.identity())))
        if name == 'board_message':
            body.update(project_id=self.project, from_agent_id=self.agent['id'], session_id=self.session)
            return self.call('/v1/messages', body)
        if name == 'board_ack':
            return self.call('/v1/messages/' + body['message_id'] + '/ack', self.identity())
        if name == 'board_knowledge':
            body['project_id'] = self.project
            return self.call('/v1/knowledge/search', body)
        actions = {'board_claim': 'claim', 'board_progress': 'progress', 'board_block': 'block',
                   'board_handoff': 'handoff', 'board_receive': 'handoff-accept',
                   'board_result': 'result', 'board_artifact': 'artifacts'}
        if name in actions:
            key = body.pop('work_id')
            body.update(self.identity())
            return self.call('/v1/work/' + key + '/' + actions[name], body)
        raise NetworkError('未知工具')


class DeviceClient(AgentClient):
    """Shared device tools do not own, start, or impersonate a chat session."""
    def __init__(self, config):
        self.config, self.endpoint = config, config['hub']

    def connect(self):
        return self.call('/v1/me')

    def close(self):
        pass


def device_loop(config, config_path):
    from .resource_worker import ResourceWorker
    resources = ResourceWorker(config_path)
    resource_thread = threading.Thread(target=resources.run, name='resource-tools', daemon=True)
    resource_thread.start()
    def stop(signum, frame):
        resources.stop.set()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    worker = config['worker']
    while not resources.stop.is_set():
        try:
            request_json(config['hub'], '/v1/devices/heartbeat', {
                'name': config.get('device_name', platform.node()), 'os': platform.system(),
                'environment_id': worker['environment_id'], 'client_version': VERSION,
                'tools': [tool for tool in ('codex', 'claude', 'hapi') if agent_executable(config, tool)],
                'runner': {'python': sys.executable, 'package_root': str(Path(__file__).resolve().parent.parent),
                           'config_path': str(Path(config_path).resolve())},
                'resource_status': resources.last_error or 'connected'})
            resources.announce()
            resources.pulse()
        except (NetworkError, OSError):
            pass
        resources.stop.wait(20)
    resource_thread.join(timeout=8)


def connection(config_path, project, workspace, name, provider, session=None):
    result = {'command': sys.executable, 'args': ['-m', 'board_network.cli', '--config', str(Path(config_path).resolve()),
            'mcp', '--project', project, '--workspace', workspace, '--name', name, '--provider', provider],
            'env': {'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}}
    if session:
        result['args'] += ['--session', session]
    return result


def launch(config, config_path, args):
    executable = agent_executable(config, args.provider)
    if not executable:
        raise NetworkError('本机未找到 ' + args.provider + '，请先安装并登录该 Agent')
    ws = args.workspace
    project = args.project
    if config['role'] == 'server':
        root = config['projects'][project]['workspaces'][ws]['root']
        if config['projects'][project]['workspaces'][ws]['device_id'] != config['worker']['device_id']:
            raise NetworkError('只能在本机工作区启动 Agent')
    else:
        if project != config['project_id'] or ws != config['workspace_id']:
            raise NetworkError('工作区与配对配置不一致')
        root = config['workspace_root']
    if not Path(root).is_dir():
        raise NetworkError('本机工作区不存在：' + root)
    spec = connection(config_path, project, ws, args.name, args.provider, args.session)
    if args.provider == 'codex':
        cmd = [executable, '-C', root]
        for key, value in spec.items():
            # JSON strings/arrays are also valid TOML values; env uses dotted keys.
            if key == 'env':
                for variable, content in value.items():
                    cmd += ['-c', 'mcp_servers.agent_board.env.' + variable + '=' + json.dumps(content)]
            else:
                cmd += ['-c', 'mcp_servers.agent_board.' + key + '=' + json.dumps(value)]
    else:
        cmd = [executable, '--mcp-config', json.dumps({'mcpServers': {'agent_board': spec}})]
    prompt = ('你已通过 agent_board MCP 接入 Agent Board。首先调用 board_status、board_inbox、board_tasks。'
              '只认领用户授权且符合本设备范围的任务；执行前读取完整目标、范围、约束和验收标准。'
              '持续报告进度，检查收件箱；交接前停止修改并确认 released；结果必须逐项给出真实验证依据。'
              '收到消息不等于已完成指令，处理后调用 board_ack。不要将目标扩大为 Git 协作。')
    cmd.append(prompt)
    return subprocess.call(cmd, cwd=root)
