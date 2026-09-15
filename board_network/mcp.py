"""Portable Agent Board: MCP stdio transport for existing agents; no secondary model or session engine."""
import json
import sys
from .client import AgentClient
from .common import NetworkError
from . import VERSION

S = {'type': 'string', 'minLength': 1}
L = {'type': 'array', 'items': S}
I = {'type': 'integer', 'minimum': 1}


def tool(name, description, fields=None, required=None):
    fields = fields or {}
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': fields, 'required': required if required is not None else list(fields),
        'additionalProperties': False}}


BASE = {'work_id': S, 'revision': I}
OWN = dict(BASE, attempt_id=S)
TOOLS = [
    tool('board_status', '查看当前会话身份、真实设备状态和同项目 Agent。'),
    tool('board_tasks', '查看任务及分配、进度、阻塞和交接。认领后才可执行。'),
    tool('board_task', '读取完整任务、约束、验收标准、事件与产物。', {'work_id': S}),
    tool('board_create', '创建已获用户授权的任务；request_id 在重试时必须保持一致。',
         {'request_id': S, 'title': S, 'goal': S, 'scope': L, 'constraints': L, 'acceptance': L, 'target_agent_id': S},
         ['request_id', 'title', 'goal', 'scope', 'constraints', 'acceptance']),
    tool('board_claim', '原子认领待办任务，返回本次 attempt_id；版本冲突时重新读取。', BASE),
    tool('board_progress', '报告已发生的进度，使用最新 revision 和本次 attempt_id。', dict(OWN, note=S)),
    tool('board_block', '报告阻塞及需要的帮助；不会自动转移执行权。', dict(OWN, note=S)),
    tool('board_handoff', '停止修改后交接同一任务，完整说明已完成、剩余和上下文；released 必须为 true。',
         dict(OWN, to_agent_id=S, completed=S, remaining=S, context={'type': 'string'}, released={'type': 'boolean'})),
    tool('board_receive', '接收发给本 Agent 的交接，生成新的执行编号，保留原目标与约束。', BASE),
    tool('board_result', '提交结果待人工验收，checks 按原验收标准顺序逐条给出真实依据。',
         dict(OWN, summary=S, checks={'type': 'array', 'items': {'type': 'object', 'properties': {
             'status': {'type': 'string', 'enum': ['passed', 'failed', 'unverified']}, 'evidence': S},
             'required': ['status', 'evidence'], 'additionalProperties': False}}, artifact_ids=L)),
    tool('board_inbox', '轮询本 Agent 未确认收件箱，记录送达；每个工作阶段和等待交接时应检查。'),
    tool('board_message', '发送给同项目 Agent；同一 request_id 的网络重试不会重复投递。任务消息对项目成员可见。',
         {'request_id': S, 'to_agent_id': S, 'body': S, 'work_id': S, 'reply_to': S, 'needs_ack': {'type': 'boolean'}},
         ['request_id', 'to_agent_id', 'body']),
    tool('board_ack', '确认已处理发给自己的消息。', {'message_id': S}),
    tool('board_knowledge', '检索授权知识，返回原文来源和当前版本校验值。', {'query': S}),
    tool('board_artifact', '上传当前执行的真实产物，base64 编码且附 SHA256，最多 1 MiB。',
         {'work_id': S, 'attempt_id': S, 'name': S, 'content': S, 'sha256': S}),
]


def validate(value, schema):
    kind = schema.get('type')
    expected = {'string': str, 'integer': int, 'boolean': bool, 'array': list, 'object': dict}[kind]
    if type(value) is not expected:
        raise NetworkError('工具参数类型错误：' + kind)
    if kind == 'object':
        if set(schema.get('required', [])) - set(value) or (schema.get('additionalProperties') is False and set(value) - set(schema['properties'])):
            raise NetworkError('工具参数缺失或包含未知字段')
        for key, part in value.items():
            if key in schema['properties']:
                validate(part, schema['properties'][key])
    if kind == 'array':
        for part in value:
            validate(part, schema['items'])
    if kind == 'string' and len(value) < schema.get('minLength', 0):
        raise NetworkError('参数不能为空')
    if kind == 'integer' and value < schema.get('minimum', value):
        raise NetworkError('参数超出范围')
    if 'enum' in schema and value not in schema['enum']:
        raise NetworkError('参数枚举值无效')


def serve(config, args):
    client = AgentClient(config, args.project, args.workspace, args.name, args.provider, args.session)
    initialized = False
    try:
        for line in sys.stdin:
            request = {}
            try:
                if len(line) > 1600000:
                    raise ValueError('request too large')
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError('request must be object')
                if 'id' not in request:
                    continue
                method = request.get('method')
                if method == 'initialize':
                    if not initialized:
                        client.connect()
                        initialized = True
                    requested = request.get('params', {}).get('protocolVersion')
                    result = {'protocolVersion': requested if requested in ('2025-06-18', '2024-11-05', '2025-03-26') else '2025-06-18',
                              'capabilities': {'tools': {}}, 'serverInfo': {'name': 'agent-board', 'version': VERSION},
                              'instructions': '以任务目标和约束为准；定期检查收件箱；执行必须先认领；交接先停止修改；结果逐项验证。'}
                elif method == 'ping':
                    result = {}
                elif not initialized:
                    raise NetworkError('请先初始化会话')
                elif method == 'tools/list':
                    result = {'tools': TOOLS}
                elif method == 'tools/call':
                    params = request.get('params', {})
                    spec = next((t for t in TOOLS if t['name'] == params.get('name')), None)
                    try:
                        if not spec:
                            raise NetworkError('未知工具')
                        arguments = params.get('arguments', {})
                        validate(arguments, spec['inputSchema'])
                        value = client.tools_call(spec['name'], arguments)
                        result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
                    except (NetworkError, KeyError, TypeError, OSError) as exc:
                        result = {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
                else:
                    raise NetworkError('不支持的 MCP 方法')
                response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
            except (ValueError, NetworkError, TypeError, KeyError) as exc:
                response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                            'error': {'code': -32600, 'message': str(exc)}}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        client.close()
    return 0
