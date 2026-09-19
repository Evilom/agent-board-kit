"""Local resource tools. Credentials and command definitions never leave the device."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

from .common import NetworkError, NoRedirect, digest, encoded, identifier, load_config, now, request_json, ssl_context, validate_url
from .resources import ACCESS, OPERATIONS


CHUNK = 512 * 1024
DENIED = {'.git', '.runtime', '.ssh', '.aws', '.azure', '.gnupg', '.secrets', '.codex', '.claude', '.mcp.json',
          'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519', '.npmrc', '.pypirc'}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(data) + b'\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def relative_path(value, allow_dot=False):
    if not isinstance(value, str) or not value or len(value) > 1000 or any(c in value for c in ('\\', ':', '\x00')):
        raise NetworkError('expected a relative path using forward slashes')
    parts = PurePosixPath(value).parts
    if value.startswith('/') or '..' in parts or (value == '.' and not allow_dot):
        raise NetworkError('path is outside resource', 403)
    for part in parts:
        lower = part.casefold()
        # These are data paths, never the credential store or shell configuration.
        if lower in DENIED or lower.startswith('.env') or lower.endswith(('.key', '.pem', '.p12', '.pfx', '.token')) or part.rstrip(' .') != part:
            raise NetworkError('credential and runtime paths are not shared', 403)
        if lower.split('.')[0] in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}:
            raise NetworkError('reserved device filename', 403)
    return str(PurePosixPath(value))


def under(relative, prefixes):
    return any(p == '.' or relative == p or relative.startswith(p.rstrip('/') + '/') for p in prefixes)


def resource_specs(config):
    resources = config.get('resources', {})
    if not isinstance(resources, dict) or len(resources) > 100:
        raise NetworkError('resources must be a mapping with at most 100 entries')
    manifests = []
    for key, spec in resources.items():
        identifier(key)
        if not isinstance(spec, dict) or not isinstance(spec.get('root'), str) or not Path(spec['root']).is_absolute():
            raise NetworkError('resource root must be an absolute local path')
        root = Path(spec['root']).resolve()
        if not root.is_dir():
            raise NetworkError('resource root is unavailable: ' + key)
        read = spec.get('read_prefixes', ['.'])
        write = spec.get('write_prefixes', [])
        context = spec.get('context_files', [])
        for prefixes in (read, write, context):
            if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
                raise NetworkError('resource prefixes must be lists')
            for p in prefixes:
                relative_path(p, allow_dot=prefixes is not context)
        if not isinstance(spec.get('access', {}), dict):
            raise NetworkError('resource access must be a mapping of device to permissions')
        for device, permissions in spec.get('access', {}).items():
            identifier(device)
            if not isinstance(permissions, list) or not set(permissions) <= ACCESS:
                raise NetworkError('invalid resource access')
        refs = spec.get('credential_refs', [])
        if not isinstance(refs, list) or not set(refs) <= set(config.get('credential_refs', {})):
            raise NetworkError('resource refers to undefined credential')
        commands = {}
        for name, command in spec.get('commands', {}).items():
            identifier(name)
            argv = command.get('argv')
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\x00' not in a for a in argv):
                raise NetworkError('resource commands require fixed argv, without shell interpolation')
            timeout = command.get('timeout', 300)
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                raise NetworkError('command timeout must be 1-3600 seconds')
            credentials = command.get('credentials', {})
            if not isinstance(credentials, dict) or not set(credentials.values()) <= set(refs) or not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env) for env in credentials):
                raise NetworkError('command credentials must reference permitted logical names')
            commands[name] = {'description': command.get('description', name), 'timeout': timeout,
                              'credentials': sorted(set(credentials.values()))}
        services = {}
        for name, service in spec.get('services', {}).items():
            identifier(name)
            if service.get('method') not in ('GET', 'POST', 'PATCH', 'PUT'):
                raise NetworkError('unsupported configured service method')
            validate_url(service['url'].split('{', 1)[0], service.get('allow_private_http', False))
            parameters = service.get('parameters', {})
            if not isinstance(parameters, dict) or not all(isinstance(v, str) and len(v) < 200 for v in parameters.values()):
                raise NetworkError('service parameters require bounded validation patterns')
            for pattern in parameters.values():
                re.compile(pattern)
            names = []
            for header in service.get('headers', {}).values():
                if isinstance(header, dict):
                    if set(header) - {'credential', 'prefix'} or header.get('credential') not in refs:
                        raise NetworkError('service header credential is not granted')
                    names.append(header['credential'])
                elif not isinstance(header, str):
                    raise NetworkError('invalid configured service header')
            for field in ('parameter_refs', 'body_bindings'):
                mapping = service.get(field, {})
                if not isinstance(mapping, dict) or not set(mapping.values()) <= set(refs):
                    raise NetworkError('service bindings require permitted references')
                names.extend(mapping.values())
            guard = service.get('guard')
            if guard:
                if not isinstance(guard, dict) or set(guard) != {'url', 'field', 'reference'} or guard['reference'] not in refs or urlsplit(guard['url']).netloc != urlsplit(service['url']).netloc:
                    raise NetworkError('service scope guard must use the same host and an allowed reference')
                names.append(guard['reference'])
            services[name] = {'description': service.get('description', name), 'method': service['method'],
                              'parameters': list(parameters), 'credentials': sorted(set(names))}
        manifest = dict(key=key, name=spec.get('name', key), project=spec.get('project', key), root=str(root),
                        description=spec.get('description', ''), aliases=spec.get('aliases', []),
                        access=spec.get('access', {}), commands=commands, services=services, context_files=context,
                        credential_refs=refs, write_prefixes=write)
        # Includes local policy, but never the contents of the referenced secret.
        manifest['revision'] = digest([spec, {k: config['credential_refs'][k] for k in refs}])
        manifests.append(manifest)
    return manifests


class LocalTools:
    def __init__(self, config, cancel=None):
        self.config = config
        self.device = config['worker']['device_id']
        self.manifests = {m['key']: m for m in resource_specs(config)}
        self.runtime = Path(config['runtime_dir']) / 'resource-operations'
        self.cancel = cancel or threading.Event()

    def path(self, spec, relative, writing=False, allow_dot=False):
        relative = relative_path(relative, allow_dot)
        prefixes = spec.get('write_prefixes', []) if writing else spec.get('read_prefixes', ['.'])
        if not under(relative, prefixes):
            raise NetworkError('path is outside authorized prefixes', 403)
        root = Path(spec['root']).resolve(strict=True)
        target = (root / relative).resolve()
        try:
            actual = target.relative_to(root).as_posix()
        except ValueError:
            raise NetworkError('symlink escapes resource root', 403) from None
        relative_path(actual, allow_dot)
        if not under(actual, prefixes):
            raise NetworkError('resolved path is outside authorized prefixes', 403)
        for provider in self.config.get('credential_refs', {}).values():
            if provider.get('kind') in ('file', 'dotenv') and target == Path(provider['path']).resolve():
                raise NetworkError('credential files cannot be read as project data', 403)
        return target

    @staticmethod
    def expected(target, expected):
        if not isinstance(expected, str) or not (expected == 'absent' or re.fullmatch('[a-f0-9]{64}', expected)):
            raise NetworkError('write requires expected_sha256 or absent')
        current = file_hash(target) if target.is_file() else 'absent'
        if target.exists() and not target.is_file():
            raise NetworkError('destination is not a regular file', 409)
        if current != expected:
            raise NetworkError('destination changed; read it before writing again', 409)

    def credential(self, reference):
        provider = self.config.get('credential_refs', {}).get(reference, {})
        kind = provider.get('kind')
        if kind == 'env':
            value = os.environ.get(provider.get('name', ''), '')
        elif kind == 'file':
            p = Path(provider['path'])
            if p.stat().st_size > 65536:
                raise NetworkError('credential source is too large')
            value = p.read_text(encoding='utf-8').strip()
        elif kind == 'dotenv':
            p = Path(provider['path'])
            if p.stat().st_size > 1024 * 1024:
                raise NetworkError('credential source is too large')
            value = ''
            name = provider['name']
            for line in p.read_text(encoding='utf-8').splitlines():
                match = re.fullmatch(r'\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*(.*?)\s*', line)
                if match:
                    value = match[1]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                        value = value[1:-1]
                    else:
                        value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        elif kind == 'keychain' and platform.system() == 'Darwin':
            cmd = ['/usr/bin/security', 'find-generic-password', '-s', provider['service'], '-w']
            if provider.get('account'):
                cmd += ['-a', provider['account']]
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            value = result.stdout.decode('utf-8').strip() if result.returncode == 0 else ''
        else:
            raise NetworkError('unsupported credential provider: ' + reference)
        if not value:
            raise NetworkError('credential unavailable: ' + reference)
        return value

    def service(self, spec, args):
        name = args.get('service')
        service = spec.get('services', {}).get(name)
        if not service:
            raise NetworkError('service operation is not configured', 403)
        parameters = args.get('parameters', {})
        if not isinstance(parameters, dict) or set(parameters) != set(service.get('parameters', {})):
            raise NetworkError('service parameters do not match configured operation')
        # Parameter references may be ordinary values from existing local configuration.
        values, used = {}, []
        for key, value in parameters.items():
            if not isinstance(value, str) or len(value) > 500 or not re.fullmatch(service['parameters'][key], value):
                raise NetworkError('invalid service path parameter: ' + key)
            values[key] = quote(value, safe='')
        for key, reference in service.get('parameter_refs', {}).items():
            value = self.credential(reference)
            values[key] = quote(value, safe=''); used.append(value)
        url = service['url'].format(**values)
        validate_url(url, service.get('allow_private_http', False))
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        for key, value in service.get('headers', {}).items():
            if isinstance(value, dict):
                secret = self.credential(value['credential']); used.append(secret)
                headers[key] = value.get('prefix', '') + secret
            else:
                headers[key] = value
        payload = copy.deepcopy(args.get('body'))
        if payload is not None and not isinstance(payload, dict):
            raise NetworkError('service body must be a JSON object')
        if service['method'] == 'GET' and payload:
            raise NetworkError('configured GET operation does not accept a body')
        if service.get('body_bindings') or service.get('body_fixed'):
            payload = payload or {}
            def bind(path, value):
                obj = payload
                parts = path.split('.')
                for part in parts[:-1]:
                    if not isinstance(obj.get(part), dict):
                        obj[part] = {}
                    obj = obj[part]
                obj[parts[-1]] = value
            for path, reference in service.get('body_bindings', {}).items():
                value = self.credential(reference); used.append(value); bind(path, value)
            for path, value in service.get('body_fixed', {}).items():
                bind(path, value)
        request = Request(url, method=service['method'], headers=headers,
                          data=encoded(payload) if payload is not None and service['method'] != 'GET' else None)
        opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl_context()))
        guard = service.get('guard')
        if guard:
            guard_url = guard['url'].format(**values)
            validate_url(guard_url, service.get('allow_private_http', False))
            try:
                with opener.open(Request(guard_url, headers=headers), timeout=20) as response:
                    data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError('guard response too large')
                value = json.loads(data)
                for part in guard['field'].split('.'):
                    value = value[part]
                expected = self.credential(guard['reference'])
                if str(value).replace('-', '') != expected.replace('-', ''):
                    raise ValueError('scope mismatch')
            except HTTPError as exc:
                exc.close()
                raise NetworkError('service target scope could not be verified', 403) from None
            except (URLError, OSError, ValueError, KeyError, TypeError):
                raise NetworkError('service target is outside the configured project or unavailable', 403) from None
        try:
            with opener.open(request, timeout=min(service.get('timeout', 30), 60)) as response:
                raw = response.read(1024 * 1024 + 1)
                status = response.status
        except HTTPError as exc:
            status = exc.code; exc.close()
            return {'service': name, 'ok': False, 'http_status': status, 'effect_uncertain': status >= 500 or status == 408,
                    'error': 'configured service rejected request; inspect upstream before repeating'}
        except (URLError, TimeoutError, OSError):
            # A POST may have succeeded upstream. It is never automatically retried.
            raise NetworkError('service response was lost; verify upstream before another request', 409) from None
        if len(raw) > 1024 * 1024:
            raise NetworkError('service response exceeded limit; verify upstream before another request', 409)
        rendered = raw.decode('utf-8', errors='replace')
        for value in sorted(used, key=len, reverse=True):
            rendered = rendered.replace(value, '[REDACTED]')
            rendered = rendered.replace(json.dumps(value, ensure_ascii=True)[1:-1], '[REDACTED]')
        try:
            result = json.loads(rendered)
        except ValueError:
            result = {'text': rendered}
        return {'service': name, 'ok': True, 'http_status': status, 'data': result}

    def command(self, spec, name):
        command = spec.get('commands', {}).get(name)
        if not command:
            raise NetworkError('command is not configured', 403)
        env = os.environ.copy()
        env.update(command.get('env', {}))
        secrets_used = []
        for variable, reference in command.get('credentials', {}).items():
            value = self.credential(reference)
            env[variable] = value; secrets_used.append(value)
        # Only fixed device-side argv are accepted. Request arguments cannot add flags.
        process = subprocess.Popen(command['argv'], cwd=spec['root'], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        output, total = bytearray(), [0]
        def drain():
            try:
                for chunk in iter(lambda: process.stdout.read(4096), b''):
                    total[0] += len(chunk)
                    if len(output) < 65536:
                        output.extend(chunk[:65536 - len(output)])
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True); reader.start()
        timed_out, cancelled = False, False
        deadline = time.monotonic() + command.get('timeout', 300)
        try:
            while process.poll() is None:
                cancelled = self.cancel.is_set()
                timed_out = time.monotonic() >= deadline
                if cancelled or timed_out:
                    raise subprocess.TimeoutExpired(command['argv'][0], command.get('timeout', 300))
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    pass
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)
        reader.join(timeout=2)
        stream_open = reader.is_alive()
        if stream_open and os.name != 'nt':
            # Stop remaining children in our group even when their parent exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reader.join(timeout=2)
        if stream_open:
            return {'command': name, 'exit_code': process.returncode, 'effect_uncertain': True,
                    'error': 'command left an open output stream; inspect process before retry'}
        rendered = bytes(output).decode('utf-8', errors='replace')
        for value in sorted(secrets_used, key=len, reverse=True):
            rendered = rendered.replace(value, '[REDACTED]')
            rendered = rendered.replace(base64.b64encode(value.encode()).decode(), '[REDACTED]')
        # Never return a cut-off credential prefix at the output limit.
        if total[0] > len(output) and secrets_used:
            rendered = '[output omitted because credential-bearing command output exceeded the limit]'
        return {'command': name, 'exit_code': process.returncode, 'output': rendered,
                'truncated': total[0] > len(output), 'timed_out': timed_out, 'cancelled': cancelled,
                'credential_refs': sorted(set(command.get('credentials', {}).values()))}

    def search(self, spec, query, key, project):
        from .knowledge import search
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise NetworkError('search query must be 1-500 characters')
        prefixes = spec.get('knowledge_prefixes', spec.get('context_files', []))
        for prefix in prefixes:
            self.path(spec, prefix, allow_dot=True)
        root = Path(spec['root']).resolve()
        return search({key: {'kind': 'documents', 'root': str(root), 'prefixes': prefixes,
                            'projects': [project]}}, project, query, 10,
                      path_guard=lambda path: self.path(spec, path.relative_to(root).as_posix()))

    def execute(self, job):
        if self.cancel.is_set():
            raise NetworkError('device is shutting down; operation did not start', 409)
        key, op, args = job['resource_key'], job['operation'], job['arguments']
        spec, manifest = self.config.get('resources', {}).get(key), self.manifests.get(key)
        if not spec or job['device_id'] != self.device or manifest['revision'] != job['resource_revision']:
            raise NetworkError('local resource policy changed or device mismatch', 409)
        permission = OPERATIONS.get(op)
        if job['requester_device'] != self.device and permission not in spec.get('access', {}).get(job['requester_device'], []):
            raise NetworkError('local resource permission denied', 403)
        allowed = {'list': {'path', 'after'}, 'read': {'path'}, 'stat': {'path'}, 'context': set(), 'search': {'query'},
                   'read_chunk': {'path', 'offset', 'size'}, 'write': {'path', 'content', 'expected_sha256'},
                   'run': {'command'}, 'request': {'service', 'parameters', 'body'}, 'upload_begin': {'path', 'size', 'sha256', 'expected_sha256'},
                   'upload_chunk': {'transfer_id', 'offset', 'content', 'sha256'}, 'upload_finish': {'transfer_id'}}
        if not isinstance(args, dict) or set(args) - allowed.get(op, set()):
            raise NetworkError('unexpected operation arguments')
        if op == 'run':
            return self.command(spec, args.get('command'))
        if op == 'request':
            return self.service(spec, args)
        if op == 'search':
            return self.search(spec, args.get('query'), key, manifest['project'])
        if op == 'context':
            files = []
            total = 0
            for relative in spec.get('context_files', []):
                target = self.path(spec, relative)
                if not target.is_file():
                    files.append({'path': relative, 'available': False}); continue
                size = target.stat().st_size
                if size > 256 * 1024 or total + size > 512 * 1024:
                    files.append({'path': relative, 'available': True, 'omitted': 'size limit'}); continue
                raw = target.read_bytes(); total += len(raw)
                files.append({'path': relative, 'source': f'resource://{job["resource_id"]}/{relative}',
                              'available': True, 'sha256': hashlib.sha256(raw).hexdigest(),
                              'text': raw.decode('utf-8', errors='replace')})
            return {'name': manifest['name'], 'description': manifest['description'], 'files': files,
                    'commands': manifest['commands'], 'services': manifest['services'], 'credential_refs': manifest['credential_refs']}
        if op.startswith('upload_'):
            return self.upload(spec, job)
        target = self.path(spec, args.get('path', '.'), writing=op == 'write', allow_dot=op == 'list')
        if op == 'list':
            if not target.is_dir():
                raise NetworkError('directory not found', 404)
            entries = []
            after = args.get('after', '')
            if not isinstance(after, str):
                raise NetworkError('invalid directory cursor')
            for child in sorted(target.iterdir(), key=lambda p: p.name):
                if child.name <= after:
                    continue
                relative = child.relative_to(Path(spec['root']).resolve()).as_posix()
                try:
                    checked = self.path(spec, relative)
                except (NetworkError, OSError):
                    continue
                entries.append({'name': child.name, 'path': relative, 'type': 'directory' if checked.is_dir() else 'file',
                                'size': checked.stat().st_size if checked.is_file() else None})
                if len(entries) == 201:
                    break
            return {'entries': entries[:200], 'next_cursor': entries[199]['name'] if len(entries) > 200 else None}
        if op == 'write':
            text = args.get('content')
            if not isinstance(text, str) or len(text.encode('utf-8')) > 1024 * 1024:
                raise NetworkError('text write is limited to 1 MiB; use chunk upload for larger files')
            self.expected(target, args.get('expected_sha256'))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Check again after directory creation and before replacement.
            target = self.path(spec, args['path'], writing=True)
            raw = text.encode('utf-8')
            fd, temp = tempfile.mkstemp(prefix='.agent-board-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                if target.is_file():
                    os.chmod(temp, target.stat().st_mode & 0o777)
                self.expected(target, args['expected_sha256'])
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            return {'path': args['path'], 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        if not target.is_file():
            raise NetworkError('file not found', 404)
        before = target.stat()
        if op == 'stat':
            result = {'path': args['path'], 'size': before.st_size, 'sha256': file_hash(target)}
        elif op == 'read':
            if before.st_size > 256 * 1024:
                raise NetworkError('file exceeds text read limit; use read_chunk')
            raw = target.read_bytes()
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise NetworkError('binary file; use read_chunk') from None
            result = {'path': args['path'], 'source': f'resource://{job["resource_id"]}/{args["path"]}',
                      'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'text': text}
        elif op == 'read_chunk':
            offset, size = args.get('offset', 0), args.get('size', CHUNK)
            if type(offset) is not int or offset < 0 or type(size) is not int or not 1 <= size <= CHUNK:
                raise NetworkError('invalid chunk range')
            with target.open('rb') as stream:
                stream.seek(offset); raw = stream.read(size)
            result = {'path': args['path'], 'offset': offset, 'size': len(raw), 'total_size': before.st_size,
                      'sha256': hashlib.sha256(raw).hexdigest(), 'content': base64.b64encode(raw).decode(),
                      'eof': offset + len(raw) >= before.st_size}
        else:
            raise NetworkError('unsupported resource operation')
        after = target.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise NetworkError('file changed during read; result is not stable', 409)
        return result

    def upload(self, spec, job):
        args, op = job['arguments'], job['operation']
        directory = self.runtime / 'transfers'
        directory.mkdir(parents=True, exist_ok=True)
        if op == 'upload_begin':
            target = self.path(spec, args.get('path'), writing=True)
            self.expected(target, args.get('expected_sha256'))
            if type(args.get('size')) is not int or not 0 <= args['size'] <= 8 * 1024**3 or not isinstance(args.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', args['sha256']):
                raise NetworkError('upload needs a size up to 8 GiB and SHA256')
            transfer = 'transfer-' + job['id'].split('-', 1)[1]
            meta = dict(args, resource_key=job['resource_key'], requested_by=job['requested_by'],
                        offset=0, complete=False)
            atomic_json(directory / (transfer + '.json'), meta)
            with (directory / (transfer + '.part')).open('xb'):
                pass
            (directory / (transfer + '.part')).chmod(0o600)
            return {'transfer_id': transfer, 'offset': 0}
        transfer = identifier(args.get('transfer_id'))
        path = directory / (transfer + '.json')
        meta = json.loads(path.read_text(encoding='utf-8'))
        if meta['resource_key'] != job['resource_key'] or meta['requested_by'] != job['requested_by']:
            raise NetworkError('upload belongs to a different caller or resource', 403)
        if meta['complete']:
            return {'transfer_id': transfer, 'complete': True, 'path': meta['path'], 'size': meta['size'], 'sha256': meta['sha256']}
        part = directory / (transfer + '.part')
        if op == 'upload_chunk':
            try:
                raw = base64.b64decode(args.get('content', ''), validate=True)
            except (ValueError, TypeError):
                raise NetworkError('invalid base64 chunk') from None
            if not raw or len(raw) > CHUNK or hashlib.sha256(raw).hexdigest() != args.get('sha256'):
                raise NetworkError('chunk size or hash mismatch')
            offset = args.get('offset')
            if type(offset) is not int or offset < 0 or offset + len(raw) > meta['size']:
                raise NetworkError('invalid upload offset')
            actual = part.stat().st_size
            if offset < actual:
                with part.open('rb') as stream:
                    stream.seek(offset)
                    if stream.read(len(raw)) != raw:
                        raise NetworkError('retry chunk differs from existing data', 409)
            elif offset == actual:
                with part.open('ab') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            else:
                raise NetworkError('upload chunk is out of order', 409)
            meta['offset'] = part.stat().st_size
            atomic_json(path, meta)
            return {'transfer_id': transfer, 'offset': meta['offset']}
        if part.stat().st_size != meta['size'] or file_hash(part) != meta['sha256']:
            raise NetworkError('complete upload size or SHA256 mismatch', 409)
        target = self.path(spec, meta['path'], writing=True)
        self.expected(target, meta['expected_sha256'])
        target.parent.mkdir(parents=True, exist_ok=True)
        # Staging and target may be on different volumes. Copy to a sibling first.
        fd, temp = tempfile.mkstemp(prefix='.agent-board-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as out, part.open('rb') as src:
                for raw in iter(lambda: src.read(1024 * 1024), b''):
                    out.write(raw)
                out.flush(); os.fsync(out.fileno())
            target = self.path(spec, meta['path'], writing=True)
            self.expected(target, meta['expected_sha256'])
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        meta['complete'] = True
        atomic_json(path, meta)
        part.unlink()
        return {'transfer_id': transfer, 'complete': True, 'path': meta['path'], 'size': meta['size'], 'sha256': meta['sha256']}


class ResourceWorker:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.runner_id = 'runner-' + uuid.uuid4().hex
        self.journal = Path(self.config['runtime_dir']) / 'resource-operations' / 'receipts'
        self.stop = threading.Event()
        self.active = None
        self.last_error = None

    def call(self, path, body=None):
        return request_json(dict(self.config['hub'], timeout=10), path, body, board_errors=True)

    def announce(self):
        self.config = load_config(self.config_path)
        return self.call('/v1/resources/announce', {'resources': resource_specs(self.config)})

    def report(self, job, receipt):
        return self.call('/v1/resource-operations/' + job['id'] + '/receipt',
                         dict(receipt, claim_token=job['claim_token']))

    def recover(self):
        for job in self.call('/v1/resource-operations/recover')['operations']:
            path = self.journal / (job['id'] + '.json')
            receipt = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            if receipt.get('status') not in ('succeeded', 'failed', 'uncertain'):
                receipt = {'status': 'uncertain', 'result': {'error': 'device restarted without a completed local receipt; operation was not repeated'}}
            self.report(job, receipt)

    def pulse(self):
        job = self.active
        if job:
            self.call('/v1/resource-operations/' + job['id'] + '/heartbeat', {'claim_token': job['claim_token']})

    def once(self):
        job = self.call('/v1/resource-operations/claim', {'runner_id': self.runner_id})['operation']
        if not job:
            return False
        path = self.journal / (job['id'] + '.json')
        self.active = job
        try:
            if path.exists():
                receipt = json.loads(path.read_text(encoding='utf-8'))
                if receipt.get('status') == 'started':
                    receipt = {'status': 'uncertain', 'result': {'error': 'previous execution may have applied; not repeated'}}
            else:
                atomic_json(path, {'status': 'started', 'started_at': now()})
                try:
                    # Reload policy immediately before the local effect.
                    result = LocalTools(load_config(self.config_path), self.stop).execute(job)
                    failed = (job['operation'] == 'run' and (result.get('exit_code') != 0 or result.get('timed_out'))) or (job['operation'] == 'request' and not result.get('ok'))
                    uncertain = result.get('effect_uncertain') or result.get('timed_out') or result.get('cancelled')
                    receipt = {'status': 'uncertain' if uncertain else 'failed' if failed else 'succeeded', 'result': result}
                except NetworkError as exc:
                    uncertain = job['operation'] == 'request' and ('response was lost' in str(exc) or 'response exceeded' in str(exc))
                    receipt = {'status': 'uncertain' if uncertain else 'failed', 'result': {'error': str(exc), 'code': exc.status}}
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    # Never expose exception bodies containing local command/env data.
                    receipt = {'status': 'uncertain', 'result': {'error': 'local operation interrupted: ' + type(exc).__name__}}
                atomic_json(path, receipt)
            self.report(job, receipt)
            return True
        finally:
            self.active = None

    def run(self):
        self.journal.mkdir(parents=True, exist_ok=True)
        lock = (self.journal.parent / 'worker.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if lock.seek(0, 2) == 0:
                    lock.write(b'0'); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            self.last_error = 'another local resource worker is already running'
            return
        try:
            self._run_locked()
        finally:
            lock.close()

    def _run_locked(self):
        recovered = False
        while not self.stop.is_set():
            try:
                if not recovered:
                    self.announce(); self.recover(); recovered = True
                self.once()
                self.last_error = None
            except (NetworkError, OSError, ValueError):
                # Recovery resends stored receipts. It never reruns an uncertain effect.
                recovered = False
                self.last_error = 'resource connection unavailable; local receipts retained'
            self.stop.wait(1)
