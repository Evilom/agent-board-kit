"""One-time local enrollment and remote tools using the existing device credential."""
import base64
import hashlib
import json
import os
import platform
import secrets
import sys
import time
from pathlib import Path

from .common import NetworkError, digest, identifier, load_config, now, request_json
from .resource_worker import CHUNK, resource_specs
from .resources import TERMINAL


def register(config_path, key, spec):
    identifier(key)
    path = Path(config_path).resolve()
    raw = path.read_bytes()
    config = json.loads(raw)
    old = config.setdefault('resources', {}).get(key)
    config['resources'][key] = spec
    # Validate resolved data without modifying the original config or knowledge.
    suffix = secrets.token_hex(4)
    temporary = path.with_name(path.name + '.resource-' + suffix)
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.chmod(0o600)
        resource_specs(load_config(temporary))
        backup = path.with_name(path.name + '.before-resource-' + suffix)
        backup.write_bytes(raw); backup.chmod(0o600)
        if backup.read_bytes() != raw or path.read_bytes() != raw:
            raise NetworkError('config changed while backing up; enrollment was not applied', 409)
        os.replace(temporary, path)
        loaded = load_config(path)
        if Path(loaded['resources'][key]['root']).resolve() != Path(spec['root']).resolve():
            raise NetworkError('resource enrollment readback failed')
    finally:
        if temporary.exists():
            temporary.unlink()
    return {'resource_key': key, 'updated': old is not None, 'config_backup': str(backup),
            'message': '设备连接将在下一次心跳加载；首次升级程序后需重启原服务。'}


def invoke(config, resource, operation, arguments, request_id, wait=30):
    endpoint = config['hub']
    job = request_json(endpoint, '/v1/resource-operations', dict(resource_id=resource, operation=operation,
                       arguments=arguments, request_id=request_id), board_errors=True)
    deadline = time.monotonic() + wait
    while job['status'] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(.25)
        job = request_json(endpoint, '/v1/resource-operations/' + job['id'], board_errors=True)
    return confirm_retrieval(endpoint, job)


def confirm_retrieval(endpoint, job):
    if job['status'] not in TERMINAL or job.get('retrieval'):
        return job
    try:
        return request_json(endpoint, '/v1/resource-operations/' + job['id'] + '/retrieved',
                            {'result_sha256': digest([job['status'], job['result']])}, board_errors=True)
    except NetworkError as exc:
        # Preserve a received result even if the acknowledgement is lost or an older
        # Hub does not support it. A reader on the executing device cannot ack for a peer.
        return dict(job, retrieval_confirmation={'recorded': False, 'code': exc.status})


def require_result(job):
    if job['status'] != 'succeeded':
        raise NetworkError('operation ' + job['id'] + ' is ' + job['status'] + '; inspect this operation before any retry', 409)
    return job['result']


def check_ecosystem(config, query, command=None, request_id=None, wait=30):
    """Run a probe as this actual device; callers opt into a named local command."""
    from . import VERSION
    from urllib.parse import urlencode
    identity = request_json(config['hub'], '/v1/me')
    if identity['device_id'] != config['worker']['device_id']:
        raise NetworkError('local device configuration and authenticated identity differ', 409)
    resources = request_json(config['hub'], '/v1/resources?' + urlencode({'query': query}))['resources']
    if len(resources) != 1:
        raise NetworkError('probe needs exactly one authorized resource; use its name or project key', 409)
    target = resources[0]
    if not target['online']:
        raise NetworkError('target resource is offline; no probe operation submitted', 409)
    request_id = request_id or 'probe-' + secrets.token_hex(12)
    identifier(request_id)
    # Bound generated keys independently of the human-facing request id length.
    prefix = 'probe-' + hashlib.sha256(request_id.encode()).hexdigest()[:48]
    report = {'checked_at': now(), 'version': VERSION, 'os': platform.system(), 'python': sys.version.split()[0],
              'requester': identity['principal_id'], 'requester_device': identity['device_id'],
              'request_id': request_id, 'target_resource': target['id'], 'target_device': target['device_id'],
              'cross_device': target['device_id'] != identity['device_id'], 'operations': [], 'passed': False}
    context = invoke(config, target['id'], 'context', {}, prefix + '-context', wait)
    report['operations'].append({'id': context['id'], 'operation': 'context', 'status': context['status'],
                                  'retrieval': context.get('retrieval'),
                                  'files': [{k: v for k, v in f.items() if k != 'text'}
                                            for f in (context.get('result') or {}).get('files', [])]})
    if context['status'] != 'succeeded' or not context.get('retrieval'):
        return report
    if command:
        if command not in context['result'].get('commands', {}):
            raise NetworkError('probe command is not offered by this resource', 403)
        job = invoke(config, target['id'], 'run', {'command': command}, prefix + '-command', wait)
        report['operations'].append({'id': job['id'], 'operation': 'run', 'status': job['status'],
                                     'retrieval': job.get('retrieval'), 'result': job['result']})
    report['passed'] = all(o['status'] == 'succeeded' and o['retrieval'] for o in report['operations'])
    return report


def copy_resource(config, source, source_path, target, target_path, expected, request_id, wait=60):
    """Resumable chunk transfer; retry the same command and request_id after a lost reply."""
    identifier(request_id)
    def call(resource, op, args, suffix):
        # Fixed-length keys permit long caller ids and bind every step to one transfer.
        key = 'copy-' + hashlib.sha256((request_id + ':' + suffix).encode()).hexdigest()[:48]
        return require_result(invoke(config, resource, op, args, key, wait))
    original = call(source, 'stat', {'path': source_path}, 'source')
    upload = call(target, 'upload_begin', dict(path=target_path, size=original['size'],
                  sha256=original['sha256'], expected_sha256=expected), 'begin')
    offset = 0
    while offset < original['size']:
        chunk = call(source, 'read_chunk', dict(path=source_path, offset=offset, size=CHUNK), 'read-' + str(offset))
        raw = base64.b64decode(chunk['content'], validate=True)
        if not raw or hashlib.sha256(raw).hexdigest() != chunk['sha256'] or chunk['total_size'] != original['size']:
            raise NetworkError('source changed or chunk integrity failed', 409)
        call(target, 'upload_chunk', dict(transfer_id=upload['transfer_id'], offset=offset,
             content=chunk['content'], sha256=chunk['sha256']), 'write-' + str(offset))
        offset += len(raw)
    # Target verifies the full original SHA256 before exposing a final file.
    return call(target, 'upload_finish', {'transfer_id': upload['transfer_id']}, 'finish')
