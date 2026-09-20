"""Read-only checks for the device connection and its local process ownership."""
import platform
from pathlib import Path

from . import VERSION
from .common import NetworkError, request_json
from .process_lock import inspect_lock


def doctor(config, config_path):
    runtime = Path(config['runtime_dir'])
    result = {'role': config['role'], 'version': VERSION, 'os': platform.system(),
              'config_path': str(Path(config_path).resolve()),
              'local_resources': sorted(config.get('resources', {})),
              'processes': {'service': inspect_lock(runtime / 'service.lock'),
                            'resource_worker': inspect_lock(runtime / 'resource-operations/worker.lock')},
              'warnings': []}
    for key, path in (('hub', '/health'), ('identity', '/v1/me'), ('authorization', '/v1/projects'),
                      ('devices', '/v1/devices'), ('resources', '/v1/resources')):
        try:
            result[key] = request_json(config['hub'], path)
        except NetworkError as exc:
            result[key] = {'available': False, 'code': exc.status}
            result['warnings'].append(key + ': connection unavailable')
    local = next((d for d in result['devices'].get('devices', [])
                  if d['id'] == config['worker']['device_id']), None)
    result['device'] = local
    if not result['processes']['resource_worker']['running']:
        result['warnings'].append('本机资源执行器未运行；启动 service 或已安装的登录服务。')
    elif not result['processes']['service']['running']:
        result['warnings'].append('资源执行器由独立进程或旧版服务运行；切换登录服务前请先停止原服务。')
    if local and local.get('resource_status') not in (None, 'connected'):
        result['warnings'].append('设备报告：' + local['resource_status'])
    if local and local.get('connection_conflict'):
        result['warnings'].append('同一设备有重叠的客户端心跳；请停止原客户端后保留一个服务入口。')
    if not config.get('resources'):
        result['warnings'].append('本机尚未登记共享工程；仍可调用已授权的其他设备工程。')
    if config.get('role') == 'server' and config.get('execution_enabled', True):
        from .dagu import Dagu
        try:
            result['workers'] = Dagu(config['dagu']).workers()
        except NetworkError as exc:
            result['workers'] = {'available': False, 'code': exc.status}
            result['warnings'].append('可选执行后端不可用。')
    return result
