"""Install the existing lightweight service at user login, without starting a model."""
import getpass
import os
import platform
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .common import NetworkError, load_config


def definition(config_path, system=None):
    system = system or platform.system()
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    root = Path(__file__).resolve().parent.parent
    runtime = Path(config['runtime_dir'])
    argv = [sys.executable, str(root / 'agent_board.py'), 'network', '--config', str(config_path), 'service']
    if system == 'Darwin':
        path = Path.home() / 'Library/LaunchAgents/local.agent-board.service.plist'
        data = plistlib.dumps({'Label': 'local.agent-board.service', 'ProgramArguments': argv,
                              'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
                              'ThrottleInterval': 10, 'StandardOutPath': str(runtime / 'service.stdout.log'),
                              'StandardErrorPath': str(runtime / 'service.stderr.log'),
                              'EnvironmentVariables': {'PYTHONUNBUFFERED': '1', 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}})
    elif system == 'Windows':
        path = runtime / 'agent-board-service.xml'
        args = subprocess.list2cmdline(argv[1:])
        user = os.environ.get('USERDOMAIN', '')
        user = (user + '\\' if user else '') + os.environ.get('USERNAME', getpass.getuser())
        # Interactive user logon: reuse the user's existing file and keychain access.
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>99</Count></RestartOnFailure></Settings>
<Actions Context="Author"><Exec><Command>{escape(argv[0])}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions></Task>'''
        data = xml.encode('utf-16')
    elif system == 'Linux':
        path = Path.home() / '.config/systemd/user/agent-board.service'
        # systemd ExecStart syntax accepts individually quoted arguments.
        command = ' '.join('"' + a.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"' for a in argv)
        data = ('[Unit]\nDescription=Agent Board device service\nAfter=network-online.target\n'
                '[Service]\nType=simple\nExecStart=' + command + '\nRestart=on-failure\nRestartSec=10\n'
                '[Install]\nWantedBy=default.target\n').encode()
    else:
        raise NetworkError('unsupported service platform')
    return path, data


def install(config_path, activate=False):
    path, data = definition(config_path)
    config = load_config(config_path)
    Path(config['runtime_dir']).mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        backup = path.with_name(path.name + '.before-' + secrets.token_hex(4))
        backup.write_bytes(path.read_bytes())
        if backup.read_bytes() != path.read_bytes():
            raise NetworkError('service backup verification failed')
    path.write_bytes(data)
    path.chmod(0o600)
    commands = []
    if activate:
        if platform.system() == 'Darwin':
            active = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/local.agent-board.service'], capture_output=True)
            if active.returncode:
                commands = [['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)]]
        elif platform.system() == 'Windows':
            commands = [['schtasks', '/Create', '/TN', 'AgentBoardService', '/XML', str(path), '/F'],
                        ['schtasks', '/Run', '/TN', 'AgentBoardService']]
        else:
            commands = [['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', '--now', 'agent-board.service']]
        for command in commands:
            result = subprocess.run(command, capture_output=True, timeout=30)
            if result.returncode:
                raise NetworkError('service activation failed; definition preserved at ' + str(path), 409)
    return {'path': str(path), 'activated': activate,
            'message': '仅运行设备连接与已授权工具，不启动模型。启用前应停止原 service，避免重复进程。'}
