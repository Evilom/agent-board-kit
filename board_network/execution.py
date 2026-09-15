"""Portable Agent Board: Fixed Dagu capabilities. Local config is the authority for paths and identity."""
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .common import NetworkError, load_config, now
from .client import agent_executable

CAPABILITIES = {
    'environment.info/v1': {'name': '环境检查', 'description': '读取系统、Python 与已安装 Agent 的版本。'},
    'workspace.inventory/v1': {'name': '目录检查', 'description': '列出授权范围内文件及 SHA256，不执行项目代码。'},
    'agent.codex.read/v1': {'name': 'Codex 只读分析', 'description': '使用本机已登录的 Codex，启用 read-only 沙箱。'},
    'agent.claude.read/v1': {'name': 'Claude Code 只读分析', 'description': '仅开放 Read、Glob、Grep 工具，禁用外部 MCP。'},
}


def excluded(parts):
    return any(part in ('.runtime', '.git', 'node_modules', '.venv', '__pycache__') or part.startswith('.env')
               or part.endswith(('.token', '.pem', '.key')) for part in parts)


def candidates(path):
    if path.is_file():
        yield path
        return
    for directory, folders, files in os.walk(path, followlinks=False):
        folders[:] = [name for name in folders if not excluded((name,)) and not (Path(directory) / name).is_symlink()]
        for name in files:
            yield Path(directory) / name


def workspace_for(config, contract):
    worker = config['worker']
    if any(contract[k] != worker[k] for k in ('device_id', 'environment_id')) or platform.system() != contract['os']:
        raise NetworkError('执行设备或系统与授权环境不一致')
    if config['role'] == 'server':
        ws = config['projects'][contract['project_id']]['workspaces'][contract['workspace_id']]
        if ws['device_id'] != worker['device_id']:
            raise NetworkError('工作区不属于本设备')
        root = Path(ws['root']).resolve(strict=True)
    else:
        if config['project_id'] != contract['project_id'] or config['workspace_id'] != contract['workspace_id']:
            raise NetworkError('执行工作区与客户端配对配置不一致')
        root = Path(config['workspace_root']).resolve(strict=True)
    return root


def run(contract, config_path=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    receipt = {k: contract[k] for k in ('task_id', 'attempt_id', 'project_id', 'workspace_id', 'device_id', 'environment_id', 'recipe')}
    receipt.update(actual_os=platform.system(), started_at=now(), exit_code=1, read_only=True)
    try:
        config = load_config(config_path or os.environ['AGENT_BOARD_CONFIG'])
        recipe = contract['recipe']
        if recipe not in CAPABILITIES or not config.get('execution_enabled', True):
            raise NetworkError('本机未授权该能力')
        if recipe.startswith('agent.') and recipe not in config.get('agent_capabilities', []):
            raise NetworkError('请先在本设备配置 agent_capabilities 授权该 Agent 的只读分析')
        root = workspace_for(config, contract)
        paths = []
        for relative in contract['scope']:
            if not isinstance(relative, str) or '..' in relative.split('/') or '\\' in relative or ':' in relative or Path(relative).is_absolute():
                raise NetworkError('范围必须是工作区内相对路径')
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
            paths.append(path)
        if recipe == 'environment.info/v1':
            result = {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(), 'tools': {}}
            for name in ('codex', 'claude'):
                executable = agent_executable(config, name)
                result['tools'][name] = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=15).stdout.strip() if executable else None
        elif recipe == 'workspace.inventory/v1':
            files, skipped, seen = [], 0, set()
            for path in paths:
                for candidate in candidates(path):
                    if len(files) >= 200 or len(seen) >= 5000:
                        skipped += 1
                        break
                    relative = candidate.relative_to(root).as_posix()
                    if excluded(candidate.relative_to(root).parts):
                        continue
                    if candidate.is_symlink() or not candidate.is_file() or relative in seen:
                        continue
                    seen.add(relative)
                    # Re-check resolved path before opening, and bound each read.
                    candidate.resolve(strict=True).relative_to(root)
                    size = candidate.stat().st_size
                    sha = None
                    if size <= 2 * 1024 * 1024:
                        with candidate.open('rb') as stream:
                            content = stream.read(2 * 1024 * 1024 + 1)
                        if len(content) == size:
                            sha = hashlib.sha256(content).hexdigest()
                    files.append({'path': relative, 'size': size, 'sha256': sha})
            result = {'files': files, 'truncated': skipped > 0, 'root': str(root)}
        else:
            provider = 'codex' if '.codex.' in recipe else 'claude'
            executable = agent_executable(config, provider)
            if not executable:
                raise NetworkError('本机未安装 ' + provider)
            prompt = ('仅进行只读分析。目标：' + contract['goal'] + '\n允许关注的相对路径：' + json.dumps(contract['scope'], ensure_ascii=False)
                      + '\n约束：' + json.dumps(contract['constraints'], ensure_ascii=False)
                      + '\n验收标准：' + json.dumps(contract['acceptance'], ensure_ascii=False)
                      + '\n请用中文返回分析、具体依据和未验证事项。不要修改文件或启动其他 Agent。')
            if provider == 'codex':
                cmd = [executable, 'exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral', '--color', 'never', '-C', str(root), '-']
            else:
                cmd = [executable, '-p', '--output-format', 'json', '--tools', 'Read,Glob,Grep', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--no-session-persistence']
            # A hard bound avoids retaining unbounded logs in memory. Dagu owns
            # the process lifetime; the provider must finish before the receipt.
            import tempfile
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
                options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                process = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=output, stderr=error, **options)
                try:
                    process.communicate(prompt.encode(), timeout=480)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise NetworkError('Agent 分析超时，未生成完整结果')
                finally:
                    if os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                output.seek(0)
                report = output.read(96000).decode('utf-8', errors='replace')
                if output.read(1):
                    raise NetworkError('Agent 输出超过 96 KiB，结果不完整')
                if process.returncode:
                    directory = Path(config['runtime_dir']) / 'agent-logs'
                    directory.mkdir(parents=True, exist_ok=True)
                    error.seek(0)
                    log_path = directory / (contract['task_id'] + '.stderr.log')
                    with log_path.open('wb') as stream:
                        stream.write(error.read(256000))
                    log_path.chmod(0o600)
                    receipt['local_diagnostic'] = str(log_path)
                    raise NetworkError('Agent 执行失败，退出码 ' + str(process.returncode) + '；请在本机检查登录和运行配置')
                if provider == 'claude':
                    response = json.loads(report)
                    if response.get('is_error') or response.get('subtype') != 'success' or not isinstance(response.get('result'), str):
                        raise NetworkError('Claude Code 未返回完整成功结果')
                    report = response['result']
                result = {'provider': provider, 'report': report, 'mode': 'read-only', 'scope_policy': '任务范围由提示约束，工具权限的边界是本机 Agent 配置'}
        receipt.update(exit_code=0, result=result)
    except (OSError, ValueError, KeyError, NetworkError, subprocess.SubprocessError) as exc:
        receipt['error'] = str(exc)
    receipt['finished_at'] = now()
    print('AGENT_BOARD_RECEIPT=' + json.dumps(receipt, ensure_ascii=False), flush=True)
    return receipt['exit_code']
