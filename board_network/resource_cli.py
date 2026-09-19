"""One-time local enrollment and remote tools using the existing device credential."""
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from .common import NetworkError, identifier, load_config, request_json
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
    return job


def require_result(job):
    if job['status'] != 'succeeded':
        raise NetworkError('operation ' + job['id'] + ' is ' + job['status'] + '; inspect this operation before any retry', 409)
    return job['result']


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
