"""Device-owned resource directory and durable, bounded remote operations.

No model is started here. Resource policy is published by the owning device and
checked again by that device before any local effect. Old Board records coexist.
"""
from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from .common import NetworkError, digest, encoded, identifier, now
from .collaboration import fresh


ACCESS = {'read', 'write', 'run'}
OPERATIONS = {'list': 'read', 'read': 'read', 'stat': 'read', 'context': 'read', 'search': 'read',
              'read_chunk': 'read', 'write': 'write', 'upload_begin': 'write',
              'upload_chunk': 'write', 'upload_finish': 'write', 'run': 'run', 'request': 'run'}
TERMINAL = {'succeeded', 'failed', 'uncertain', 'expired', 'cancelled'}


def future(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def public_job(job, summary=False):
    result = {k: v for k, v in job.items() if k not in ('claim_token', 'arguments', 'receipt_hash')}
    if summary and isinstance(result.get('result'), dict):
        result['result'] = {k: v for k, v in result['result'].items() if k in
                            ('error', 'code', 'exit_code', 'http_status', 'size', 'sha256', 'timed_out', 'cancelled', 'path')}
    return result


class Resources:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS device_resources (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_operations (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, resource TEXT NOT NULL, device TEXT NOT NULL,
                    data TEXT NOT NULL, UNIQUE(principal, request_key));
                CREATE INDEX IF NOT EXISTS resource_operations_device ON resource_operations(device);
                CREATE INDEX IF NOT EXISTS resource_operations_pending ON resource_operations(device,json_extract(data,'$.status'));
            ''')

    @staticmethod
    def _resource(db, key):
        row = db.execute('SELECT data FROM device_resources WHERE id=?', (identifier(key),)).fetchone()
        if not row:
            raise NetworkError('resource not found', 404)
        return json.loads(row[0])

    @staticmethod
    def _job(db, key):
        row = db.execute('SELECT data FROM resource_operations WHERE id=?', (identifier(key),)).fetchone()
        if not row:
            raise NetworkError('operation not found', 404)
        return json.loads(row[0])

    def _save(self, db, job, event=None):
        job['updated_at'] = now()
        db.execute('UPDATE resource_operations SET data=? WHERE id=?', (encoded(job).decode(), job['id']))
        if event:
            self.store._audit(db, job['id'], job['requested_by'], event)

    @staticmethod
    def permissions(actor, resource):
        # A read-only principal sharing a device id cannot acquire write authority.
        if not any('collaborate' in g.get('operations', []) for g in actor[1].get('grants', {}).values()):
            return []
        if actor[1]['device_id'] == resource['device_id']:
            return sorted(ACCESS)
        return resource['access'].get(actor[1]['device_id'], [])

    def authorize(self, actor, resource, access=None):
        permissions = self.permissions(actor, resource)
        if not permissions or (access and access not in permissions):
            raise NetworkError('resource operation is not granted', 403)
        if not resource.get('enabled', True):
            raise NetworkError('resource is no longer shared', 409)
        return permissions

    def announce(self, actor, body):
        if not any('collaborate' in g.get('operations', []) for g in actor[1].get('grants', {}).values()):
            raise NetworkError('device may not publish resources', 403)
        items = body.get('resources')
        if set(body) != {'resources'} or not isinstance(items, list) or len(items) > 100:
            raise NetworkError('expected at most 100 device resources')
        device = actor[1]['device_id']
        known_devices = {p['device_id'] for p in self.hub.config['principals'].values() if not p.get('disabled')}
        records, seen = [], set()
        for item in items:
            required = {'key', 'name', 'project', 'root', 'description', 'aliases', 'revision',
                        'access', 'commands', 'services', 'context_files', 'credential_refs', 'write_prefixes'}
            if not isinstance(item, dict) or set(item) != required:
                raise NetworkError('invalid resource manifest')
            identifier(item['key']); identifier(item['project'])
            if item['key'] in seen:
                raise NetworkError('duplicate resource key')
            seen.add(item['key'])
            if not all(isinstance(item[k], str) and len(item[k]) <= 4000 for k in ('name', 'root', 'description')):
                raise NetworkError('invalid resource description')
            if not re.fullmatch('[a-f0-9]{64}', item['revision']):
                raise NetworkError('invalid resource revision')
            for field in ('aliases', 'context_files', 'credential_refs', 'write_prefixes'):
                if not isinstance(item[field], list) or len(item[field]) > 100 or not all(isinstance(s, str) and len(s) <= 500 for s in item[field]):
                    raise NetworkError('invalid resource ' + field)
            if not isinstance(item['access'], dict) or set(item['access']) - known_devices:
                raise NetworkError('resource access must name paired devices')
            for permissions in item['access'].values():
                if not isinstance(permissions, list) or not set(permissions) <= ACCESS:
                    raise NetworkError('invalid resource permissions')
            if not isinstance(item['commands'], dict) or len(item['commands']) > 50:
                raise NetworkError('invalid resource commands')
            for key, command in item['commands'].items():
                identifier(key)
                if not isinstance(command, dict) or set(command) != {'description', 'timeout', 'credentials'}:
                    raise NetworkError('invalid command metadata')
                if not isinstance(command['description'], str) or len(command['description']) > 1000 or type(command['timeout']) is not int or not 1 <= command['timeout'] <= 3600:
                    raise NetworkError('invalid command limits')
                if not isinstance(command['credentials'], list) or not set(command['credentials']) <= set(item['credential_refs']):
                    raise NetworkError('unknown credential reference')
            if not isinstance(item['services'], dict) or len(item['services']) > 50:
                raise NetworkError('invalid resource services')
            for key, service in item['services'].items():
                identifier(key)
                if not isinstance(service, dict) or set(service) != {'description', 'method', 'parameters', 'credentials'}:
                    raise NetworkError('invalid service metadata')
                if not isinstance(service['description'], str) or not isinstance(service['parameters'], list) or not isinstance(service['credentials'], list) or not set(service['credentials']) <= set(item['credential_refs']):
                    raise NetworkError('invalid service metadata')
            record = dict(item, id='resource-' + digest([device, item['key']])[:32], device_id=device,
                          principal_id=actor[0], last_seen=now(), enabled=True)
            records.append(record)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active_ids = {r['id'] for r in records}
            for row in db.execute('SELECT data FROM device_resources WHERE device=?', (device,)).fetchall():
                old = json.loads(row[0])
                if old['id'] not in active_ids:
                    old['enabled'] = False
                    db.execute('UPDATE device_resources SET data=? WHERE id=?', (encoded(old).decode(), old['id']))
            for record in records:
                db.execute('INSERT OR REPLACE INTO device_resources VALUES(?,?,?)', (record['id'], device, encoded(record).decode()))
        return {'resource_ids': [r['id'] for r in records]}

    def catalog(self, actor, query=''):
        if not isinstance(query, str) or len(query) > 500:
            raise NetworkError('invalid resource query')
        result = []
        with self.store.connect() as db:
            for row in db.execute('SELECT data FROM device_resources ORDER BY id'):
                item = json.loads(row[0])
                permissions = self.permissions(actor, item)
                if not permissions or not item['enabled']:
                    continue
                if query.casefold() not in ' '.join([item['name'], item['project'], item['device_id']] + item['aliases']).casefold():
                    continue
                permissions = [p for p in permissions if (p != 'write' or item['write_prefixes']) and
                               (p != 'run' or item['commands'] or item['services'])]
                result.append(dict({k: v for k, v in item.items() if k not in ('access', 'principal_id')},
                                   permissions=permissions, online=fresh(item['last_seen']),
                                   operations=[op for op, access in OPERATIONS.items() if access in permissions]))
        return {'resources': result}

    def submit(self, actor, body):
        if set(body) - {'request_id', 'resource_id', 'operation', 'arguments', 'work_id', 'ttl_seconds'}:
            raise NetworkError('unexpected remote operation fields')
        key, resource_id = identifier(body.get('request_id')), identifier(body.get('resource_id'))
        op, args = body.get('operation'), body.get('arguments', {})
        ttl = body.get('ttl_seconds', 3600)
        if op not in OPERATIONS or not isinstance(args, dict) or len(encoded(args)) > 1500000:
            raise NetworkError('invalid remote operation')
        if type(ttl) is not int or not 30 <= ttl <= 86400:
            raise NetworkError('operation lifetime must be 30-86400 seconds')
        if body.get('work_id'):
            work = self.hub.collaboration.work_detail(actor, identifier(body['work_id']))
            if work['status'] in ('done', 'cancelled'):
                raise NetworkError('cannot attach an operation to closed work', 409)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            resource = self._resource(db, resource_id)
            self.authorize(actor, resource, OPERATIONS[op])
            request = dict(resource_id=resource_id, operation=op, arguments=args,
                           work_id=body.get('work_id'), ttl_seconds=ttl)
            row = db.execute('SELECT request_hash,data FROM resource_operations WHERE principal=? AND request_key=?', (actor[0], key)).fetchone()
            if row:
                if row[0] != digest(request):
                    raise NetworkError('request_id already belongs to different input', 409)
                return public_job(self._expire(db, json.loads(row[1])))
            job = dict(request, id='operation-' + uuid.uuid4().hex, resource_key=resource['key'],
                       resource_revision=resource['revision'], requested_by=actor[0],
                       requester_device=actor[1]['device_id'], device_id=resource['device_id'],
                       status='queued', created_at=now(), updated_at=now(), expires_at=future(ttl),
                       started_at=None, finished_at=None, result=None)
            db.execute('INSERT INTO resource_operations VALUES(?,?,?,?,?,?,?)',
                       (job['id'], actor[0], key, digest(request), resource_id, resource['device_id'], encoded(job).decode()))
            self.store._audit(db, job['id'], actor[0], 'resource.queued')
            return public_job(job)

    def _expire(self, db, job):
        if job['status'] == 'queued' and job['expires_at'] < now():
            job.update(status='expired', finished_at=now(), result={'error': 'device did not start operation before deadline'})
            self._save(db, job, 'resource.expired')
        elif job['status'] == 'running' and job['lease_until'] < now():
            job.update(status='uncertain', result={'error': 'execution heartbeat expired; inspect result before any new request'})
            self._save(db, job, 'resource.uncertain')
        return job

    def get(self, actor, key):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, key)
            resource = self._resource(db, job['resource_id'])
            self.authorize(actor, resource, OPERATIONS[job['operation']])
            if actor[0] != job['requested_by'] and actor[1]['device_id'] != job['device_id']:
                raise NetworkError('operation is private to requester and executing device', 403)
            return public_job(self._expire(db, job))

    def recent(self, actor):
        result = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT data FROM resource_operations WHERE principal=? OR device=? ORDER BY rowid DESC LIMIT 100', (actor[0], actor[1]['device_id'])).fetchall():
                job = json.loads(row[0])
                try:
                    self.authorize(actor, self._resource(db, job['resource_id']), OPERATIONS[job['operation']])
                except NetworkError:
                    continue
                result.append(public_job(self._expire(db, job), summary=True))
        return {'operations': result}

    def claim(self, actor, body):
        runner = identifier(body.get('runner_id'))
        device = actor[1]['device_id']
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT data FROM resource_operations WHERE device=? AND json_extract(data,'$.status') IN ('queued','running') ORDER BY rowid", (device,)).fetchall():
                job = self._expire(db, json.loads(row[0]))
                if job['status'] != 'queued':
                    continue
                resource = self._resource(db, job['resource_id'])
                if resource['principal_id'] != actor[0]:
                    continue
                requester = self.hub.config['principals'].get(job['requested_by'])
                try:
                    if not requester or requester.get('disabled'):
                        raise NetworkError('requester authorization was revoked', 403)
                    self.authorize((job['requested_by'], requester), resource, OPERATIONS[job['operation']])
                    if resource['revision'] != job['resource_revision']:
                        raise NetworkError('resource policy changed; submit a new explicit request', 409)
                except NetworkError as exc:
                    job.update(status='failed', finished_at=now(), result={'error': str(exc)})
                    self._save(db, job, 'resource.policy_rejected')
                    continue
                job.update(status='running', started_at=now(), lease_until=future(90),
                           runner_id=runner, executing_principal=actor[0], claim_token=secrets.token_urlsafe(32))
                self._save(db, job, 'resource.started')
                return {'operation': job}
        return {'operation': None}

    def recover(self, actor):
        with self.store.connect() as db:
            jobs = [json.loads(r[0]) for r in db.execute('SELECT data FROM resource_operations WHERE device=?', (actor[1]['device_id'],))]
        return {'operations': [j for j in jobs if j['status'] in ('running', 'uncertain') and j.get('executing_principal') == actor[0]]}

    def receipt(self, actor, key, action, body):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, key)
            if actor[1]['device_id'] != job['device_id'] or actor[0] != job.get('executing_principal') or not secrets.compare_digest(str(body.get('claim_token', '')), job.get('claim_token', '-')):
                raise NetworkError('only the owning device execution may report this operation', 403)
            if action == 'heartbeat':
                if job['status'] == 'running':
                    job['lease_until'] = future(90)
                    self._save(db, job)
                return public_job(job)
            status, result = body.get('status'), body.get('result')
            if status not in ('succeeded', 'failed', 'uncertain') or not isinstance(result, dict) or len(encoded(result)) > 1500000:
                raise NetworkError('invalid operation receipt')
            fingerprint = digest([status, result])
            if job['status'] in ('succeeded', 'failed'):
                if job.get('receipt_hash') != fingerprint:
                    raise NetworkError('terminal receipt cannot be replaced', 409)
                return public_job(job)
            job.update(status=status, result=result, receipt_hash=fingerprint, finished_at=now())
            self._save(db, job, 'resource.' + status)
            return public_job(job)

    def cancel(self, actor, key):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, key)
            if job['requested_by'] != actor[0]:
                raise NetworkError('only requester may cancel pending operation', 403)
            if job['status'] != 'queued':
                raise NetworkError('only an operation that has not started can be cancelled', 409)
            job.update(status='cancelled', finished_at=now())
            self._save(db, job, 'resource.cancelled')
            return public_job(job)

    def route(self, actor, method, path, query, body):
        if path == '/v1/resources' and method == 'GET':
            return self.catalog(actor, (query.get('query') or [''])[0])
        if path == '/v1/resources/announce' and method == 'POST':
            return self.announce(actor, body)
        if path == '/v1/resource-operations':
            return self.submit(actor, body) if method == 'POST' else self.recent(actor)
        if path == '/v1/resource-operations/claim' and method == 'POST':
            return self.claim(actor, body)
        if path == '/v1/resource-operations/recover' and method == 'GET':
            return self.recover(actor)
        match = re.fullmatch(r'/v1/resource-operations/([a-zA-Z0-9_.-]+)(?:/(heartbeat|receipt|cancel))?', path)
        if match:
            key, action = match.groups()
            if method == 'GET' and action is None:
                return self.get(actor, key)
            if method == 'POST' and action == 'cancel':
                return self.cancel(actor, key)
            if method == 'POST' and action in ('heartbeat', 'receipt'):
                return self.receipt(actor, key, action, body)
        raise NetworkError('resource route not found', 404)
