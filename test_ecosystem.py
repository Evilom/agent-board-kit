"""Resource-level integration: no running peer chat or model is required."""
import base64
import copy
import hashlib
import json
import os
import sys
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import test_collaboration as base
from board_network.common import NetworkError, digest, load_config, request_json
from board_network.hub import Hub, make_server
from board_network.resource_cli import check_ecosystem, copy_resource, invoke, register
from board_network.resource_worker import LocalTools, ResourceWorker, atomic_json, resource_specs
from board_network.process_lock import InstanceLock, inspect_lock
from board_network.service_install import definition


class EcosystemTests(unittest.TestCase):
    actor = base.CollaborationTests.actor
    register_agent = base.CollaborationTests.register
    register = base.CollaborationTests.register

    def setUp(self):
        base.CollaborationTests.setUp(self)
        self.site = self.root / 'site'; self.site.mkdir()
        (self.site / 'posts').mkdir()
        (self.site / 'README.md').write_text('网站流程：新增文章后运行 build。', encoding='utf-8')
        (self.site / '.env').write_text('NEVER_RETURN=private', encoding='utf-8')
        self.secret = self.root / 'existing-secret'
        self.secret.write_text('test-secret-do-not-publish', encoding='utf-8')
        self.config.update(runtime_dir=str(self.root / 'runtime'), worker={'device_id': 'mac', 'environment_id': 'mac-native'},
            hub={'url': 'http://127.0.0.1:8940', 'token_file': str(self.root / 'mac.token')},
            credential_refs={'cdn': {'kind': 'file', 'path': str(self.secret)}}, resources={'myweb': {
                'root': str(self.site), 'project': 'myweb', 'name': 'MyWeb', 'aliases': ['博客'],
                'description': 'Mac 上的网站', 'access': {'pc': ['read', 'write', 'run']},
                'context_files': ['README.md'], 'read_prefixes': ['.'], 'write_prefixes': ['posts'],
                'credential_refs': ['cdn'], 'commands': {'check': {
                    'argv': [sys.executable, '-c', 'import os; print("checked", os.environ["CDN_KEY"])'],
                    'credentials': {'CDN_KEY': 'cdn'}, 'timeout': 10}}}})
        self.config_path = self.root / 'resource-config.json'
        atomic_json(self.config_path, self.config)
        self.r = self.hub.resources
        self.r.announce(self.mac, {'resources': resource_specs(self.config)})
        self.resource = self.r.catalog(self.pc)['resources'][0]['id']

    def tearDown(self):
        base.CollaborationTests.tearDown(self)

    def submit(self, op='context', args=None, key='request'):
        return self.r.submit(self.pc, {'request_id': key, 'resource_id': self.resource,
                                      'operation': op, 'arguments': args or {}})

    def execute(self, op='context', args=None, key='request'):
        pending = self.submit(op, args, key)
        job = self.r.claim(self.mac, {'runner_id': 'test-runner'})['operation']
        self.assertEqual(job['id'], pending['id'])
        result = LocalTools(self.config).execute(job)
        self.r.receipt(self.mac, job['id'], 'receipt', {'claim_token': job['claim_token'], 'status': 'succeeded', 'result': result})
        return self.r.get(self.pc, job['id'])

    def test_discovery_cross_project_and_context_source(self):
        # Source Agent project p has no MyWeb workspace; explicit device resource ACL bridges it.
        self.assertNotIn('myweb', self.config['projects'])
        resource = self.r.catalog(self.pc, '博客')['resources'][0]
        self.assertTrue(resource['online'])
        self.assertEqual(resource['device_id'], 'mac')
        self.assertEqual(self.r.catalog(self.reader)['resources'], [])
        self.assertNotIn('argv', json.dumps(resource))
        self.assertNotIn(str(self.secret), json.dumps(resource))
        result = self.execute()['result']
        self.assertIn('网站流程', result['files'][0]['text'])
        self.assertEqual(result['files'][0]['sha256'], hashlib.sha256((self.site / 'README.md').read_bytes()).hexdigest())
        self.assertTrue(result['files'][0]['source'].startswith('resource://'))

    def test_publication_cannot_impersonate_other_device_or_expose_secret_fields(self):
        manifest = resource_specs(self.config)[0]
        with self.assertRaises(NetworkError):
            self.r.announce(self.reader, {'resources': [manifest]})
        bad = dict(manifest, credential_values={'cdn': 'secret'})
        with self.assertRaises(NetworkError):
            self.r.announce(self.mac, {'resources': [bad]})
        self.r.announce(self.pc, {'resources': [manifest]})
        resources = self.r.catalog(self.pc)['resources']
        self.assertEqual(len({r['id'] for r in resources}), 2)
        self.assertEqual({r['device_id'] for r in resources}, {'mac', 'pc'})

    def test_queue_idempotence_concurrent_claim_and_private_result(self):
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.submit(), range(8)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        with self.assertRaises(NetworkError):
            self.submit('read', {'path': 'README.md'})
        with ThreadPoolExecutor(8) as pool:
            claims = list(pool.map(lambda n: self.r.claim(self.mac, {'runner_id': 'runner-' + str(n)}), range(8)))
        self.assertEqual(sum(bool(c['operation']) for c in claims), 1)
        claimed = next(c['operation'] for c in claims if c['operation'])
        self.assertNotIn('claim_token', self.r.get(self.pc, claimed['id']))
        with self.assertRaises(NetworkError):
            self.r.get(self.reader, claimed['id'])
        with self.assertRaises(NetworkError):
            self.r.receipt(self.pc, claimed['id'], 'receipt', {'claim_token': claimed['claim_token'], 'status': 'succeeded', 'result': {}})
        same_device_reader = ('different-principal', dict(self.reader[1], device_id='mac'))
        self.assertEqual(self.r.recover(same_device_reader)['operations'], [])
        with self.assertRaises(NetworkError):
            self.r.receipt(same_device_reader, claimed['id'], 'receipt', {'claim_token': claimed['claim_token'], 'status': 'succeeded', 'result': {}})

    def test_file_write_requires_fresh_hash_and_respects_scope(self):
        result = self.execute('write', {'path': 'posts/new.md', 'content': 'hello 中文', 'expected_sha256': 'absent'})['result']
        self.assertEqual((self.site / 'posts/new.md').read_text(encoding='utf-8'), 'hello 中文')
        pending = self.submit('write', {'path': 'posts/new.md', 'content': 'overwrite', 'expected_sha256': 'absent'}, 'conflict')
        job = self.r.claim(self.mac, {'runner_id': 'test'})['operation']
        with self.assertRaises(NetworkError) as exc:
            LocalTools(self.config).execute(job)
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(hashlib.sha256((self.site / 'posts/new.md').read_bytes()).hexdigest(), result['sha256'])

    def test_path_traversal_credentials_and_symlinks_denied(self):
        tools = LocalTools(self.config); spec = self.config['resources']['myweb']
        for path in ('../existing-secret', '/etc/passwd', 'C:/Users/key', '.env', '.env.local', '.git/config', 'posts/key.pem', 'posts/file:secret', 'posts/CON.txt', 'posts/a. '):
            with self.subTest(path=path), self.assertRaises(NetworkError):
                tools.path(spec, path)
        with self.assertRaises(NetworkError):
            tools.path(spec, 'README.md', writing=True)
        if os.name != 'nt':
            (self.site / 'posts/escape').symlink_to(self.root, target_is_directory=True)
            (self.site / 'posts/secret-alias').symlink_to(self.secret)
            for path in ('posts/escape/existing-secret', 'posts/secret-alias'):
                with self.assertRaises(NetworkError):
                    tools.path(spec, path)
        result = self.execute('list', {'path': '.'})['result']
        self.assertNotIn('.env', [e['name'] for e in result['entries']])

    def test_command_uses_existing_credential_but_only_returns_reference(self):
        before = self.secret.read_bytes()
        result = self.execute('run', {'command': 'check'})['result']
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('[REDACTED]', result['output'])
        self.assertNotIn(before.decode(), json.dumps(result))
        self.assertEqual(self.secret.read_bytes(), before)
        self.assertEqual(result['credential_refs'], ['cdn'])

    def test_service_binding_scope_guard_and_secret_redaction(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                payload = {'parent': {'database_id': 'allowed'}}
                if self.path.endswith('/outside'):
                    payload['parent']['database_id'] = 'other'
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((payload, self.headers['Authorization']))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'payload': payload, 'echo': self.headers['Authorization']}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        root = 'http://127.0.0.1:' + str(server.server_address[1])
        config = copy.deepcopy(self.config)
        config['credential_refs']['database'] = {'kind': 'env', 'name': 'BOARD_TEST_DB'}
        spec = config['resources']['myweb']; spec['credential_refs'].append('database')
        spec['services'] = {'draft': {'url': root + '/pages/{page}', 'method': 'POST', 'allow_private_http': True,
            'parameters': {'page': '[a-z]+'}, 'headers': {'Authorization': {'credential': 'cdn', 'prefix': 'Bearer '}},
            'guard': {'url': root + '/check/{page}', 'field': 'parent.database_id', 'reference': 'database'},
            'body_bindings': {'parent.database_id': 'database'}, 'body_fixed': {'published': False}}}
        try:
            with patch.dict(os.environ, {'BOARD_TEST_DB': 'allowed'}):
                tools = LocalTools(config)
                result = tools.service(spec, {'service': 'draft', 'parameters': {'page': 'inside'},
                                             'body': {'parent': {'database_id': 'malicious'}, 'published': True}})
                self.assertEqual(result['http_status'], 200)
                self.assertNotIn(self.secret.read_text(), json.dumps(result))
                self.assertEqual(received[0][0], {'parent': {'database_id': 'allowed'}, 'published': False})
                self.assertEqual(received[0][1], 'Bearer ' + self.secret.read_text())
                with self.assertRaises(NetworkError):
                    tools.service(spec, {'service': 'draft', 'parameters': {'page': 'outside'}, 'body': {}})
                self.assertEqual(len(received), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_command_timeout_and_service_shutdown_stop_execution(self):
        spec = self.config['resources']['myweb']
        spec['commands']['wait'] = {'argv': [sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(60)'], 'timeout': 1}
        result = LocalTools(self.config).command(spec, 'wait')
        self.assertTrue(result['timed_out'])
        self.assertNotEqual(result['exit_code'], 0)
        spec['commands']['wait']['timeout'] = 60
        stop = threading.Event()
        timer = threading.Timer(.3, stop.set); timer.start()
        try:
            result = LocalTools(self.config, stop).command(spec, 'wait')
            self.assertTrue(result['cancelled'])
            self.assertNotEqual(result['exit_code'], 0)
            with self.assertRaisesRegex(NetworkError, 'did not start'):
                LocalTools(self.config, stop).execute({})
        finally:
            timer.cancel(); timer.join()

    def test_shared_knowledge_keeps_distinct_sources_and_respects_resource_acl(self):
        (self.site / 'posts/one.md').write_text('共享原文 first', encoding='utf-8')
        (self.site / 'posts/two.md').write_text('共享原文 second', encoding='utf-8')
        self.config['resources']['myweb']['knowledge_prefixes'] = ['posts']
        self.r.announce(self.mac, {'resources': resource_specs(self.config)})
        found = self.hub.route(self.pc, 'POST', '/v1/shared-knowledge/search', {}, {'query': '共享原文'})
        self.assertEqual(len(found['results']), 2)
        self.assertTrue(all(x['version'].startswith('sha256:') for x in found['results']))
        denied = self.hub.route(self.reader, 'POST', '/v1/shared-knowledge/search', {}, {'query': '共享原文'})
        self.assertEqual(denied['results'], [])

    def test_knowledge_search_cannot_bypass_file_credential_policy(self):
        (self.site / 'posts/ordinary.txt').write_text('needle public information', encoding='utf-8')
        (self.site / 'posts/.env.txt').write_text('needle private credential', encoding='utf-8')
        if os.name != 'nt':
            private = self.site / 'posts/credential.txt'
            private.write_text('needle referenced secret', encoding='utf-8')
            self.config['credential_refs']['cdn']['path'] = str(private)
            (self.site / 'posts/alias.txt').symlink_to(private)
        self.config['resources']['myweb']['knowledge_prefixes'] = ['posts']
        self.r.announce(self.mac, {'resources': resource_specs(self.config)})
        found = self.hub.route(self.pc, 'POST', '/v1/shared-knowledge/search', {}, {'query': 'needle'})
        self.assertEqual(len(found['results']), 1)
        self.assertTrue(found['results'][0]['uri'].endswith('/ordinary.txt'))
        remote = self.execute('search', {'query': 'needle'})['result']
        self.assertEqual(len(remote['results']), 1)
        self.assertTrue(remote['results'][0]['uri'].endswith('/ordinary.txt'))

    def test_shared_search_reuses_existing_sources_once_across_project_grants(self):
        self.config['knowledge_sources']['existing'] = {'kind': 'documents', 'root': str(self.site),
            'prefixes': ['README.md'], 'projects': ['p', 'private']}
        self.config['principals']['pc']['grants']['private'] = {'operations': ['read', 'query'], 'workspaces': []}
        self.config['resources']['myweb']['knowledge_prefixes'] = []
        found = self.hub.route(self.pc, 'POST', '/v1/shared-knowledge/search', {}, {'query': '网站'})
        self.assertEqual(len(found['results']), 1)
        self.assertEqual(found['results'][0]['source_id'], 'existing')
        self.assertIn('网站流程', found['results'][0]['snippet'])
        self.assertTrue(found['results'][0]['version'].startswith('sha256:'))

    def test_device_mcp_initialization_does_not_create_or_own_a_chat_agent(self):
        server = make_server(self.config, ('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        config = dict(self.config, hub={'url': 'http://127.0.0.1:' + str(server.server_address[1]), 'token_file': str(self.root / 'pc.token')})
        atomic_json(self.config_path, config)
        before = self.c.list_agents(self.mac, 'p')
        process = subprocess.Popen([sys.executable, 'agent_board.py', 'network', '--config', str(self.config_path), 'ecosystem-mcp'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            def rpc(number, method, params=None):
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}}) + '\n'); process.stdin.flush()
                return json.loads(process.stdout.readline())['result']
            rpc(1, 'initialize', {'protocolVersion': '2025-06-18'})
            names = [t['name'] for t in rpc(2, 'tools/list')['tools']]
            self.assertEqual(len(names), 5); self.assertIn('board_resources', names); self.assertNotIn('board_claim', names)
            response = rpc(3, 'tools/call', {'name': 'board_resources', 'arguments': {'query': 'MyWeb'}})
            self.assertEqual(json.loads(response['content'][0]['text'])['resources'][0]['id'], self.resource)
            self.assertEqual(self.c.list_agents(self.mac, 'p'), before)
        finally:
            process.stdin.close(); process.wait(timeout=5); process.stdout.close(); process.stderr.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_policy_revocation_before_claim_and_before_local_effect(self):
        self.submit('write', {'path': 'posts/no.md', 'content': 'x', 'expected_sha256': 'absent'})
        updated = copy.deepcopy(self.config)
        updated['resources']['myweb']['access']['pc'] = ['read']
        self.r.announce(self.mac, {'resources': resource_specs(updated)})
        self.assertIsNone(self.r.claim(self.mac, {'runner_id': 'run'})['operation'])
        self.assertFalse((self.site / 'posts/no.md').exists())
        self.r.announce(self.mac, {'resources': resource_specs(self.config)})
        self.submit('context', key='new')
        job = self.r.claim(self.mac, {'runner_id': 'run'})['operation']
        with self.assertRaises(NetworkError):
            LocalTools(updated).execute(job)

    def test_expired_lease_not_reexecuted_and_real_receipt_can_resolve(self):
        pending = self.submit()
        job = self.r.claim(self.mac, {'runner_id': 'run'})['operation']
        with self.hub.store.connect() as db:
            job['lease_until'] = '2000-01-01T00:00:00+00:00'
            self.r._save(db, job)
        self.assertEqual(self.r.get(self.pc, pending['id'])['status'], 'uncertain')
        self.assertIsNone(self.r.claim(self.mac, {'runner_id': 'next'})['operation'])
        body = {'claim_token': job['claim_token'], 'status': 'succeeded', 'result': {'real': True}}
        self.r.receipt(self.mac, job['id'], 'receipt', body)
        self.r.receipt(self.mac, job['id'], 'receipt', body)
        with self.assertRaises(NetworkError):
            self.r.receipt(self.mac, job['id'], 'receipt', dict(body, result={'real': False}))

    def test_chunk_upload_hashes_retries_and_atomic_finish(self):
        raw = bytes(range(256)) * 4000
        info = self.execute('upload_begin', {'path': 'posts/image.bin', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'expected_sha256': 'absent'})['result']
        self.assertFalse((self.site / 'posts/image.bin').exists())
        for offset in range(0, len(raw), 512 * 1024):
            chunk = raw[offset:offset + 512 * 1024]
            args = {'transfer_id': info['transfer_id'], 'offset': offset, 'content': base64.b64encode(chunk).decode(), 'sha256': hashlib.sha256(chunk).hexdigest()}
            self.execute('upload_chunk', args, 'part-' + str(offset))
            self.execute('upload_chunk', args, 'retry-' + str(offset))
        result = self.execute('upload_finish', {'transfer_id': info['transfer_id']}, 'finish')['result']
        self.assertTrue(result['complete'])
        self.assertEqual((self.site / 'posts/image.bin').read_bytes(), raw)
        chunk = self.execute('read_chunk', {'path': 'posts/image.bin', 'offset': 0}, 'read-chunk')['result']
        self.assertIn('content', chunk)
        self.assertNotIn(chunk['content'], json.dumps(self.r.recent(self.pc)))

    def test_enrollment_backup_and_service_definitions_keep_existing_credentials(self):
        before = self.config_path.read_bytes()
        spec = dict(self.config['resources']['myweb'], name='另一个工程')
        result = register(self.config_path, 'another', spec)
        self.assertEqual(Path(result['config_backup']).read_bytes(), before)
        self.assertEqual(load_config(self.config_path)['credential_refs'], self.config['credential_refs'])
        for system in ('Darwin', 'Windows', 'Linux'):
            path, data = definition(self.config_path, system)
            self.assertNotIn(self.secret.read_bytes(), data)
            self.assertIn('service', data.decode('utf-16' if system == 'Windows' else 'utf-8'))

    def test_http_device_worker_without_peer_chat_and_lost_receipt_recovery(self):
        server = make_server(self.config, ('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        endpoint = {'url': 'http://127.0.0.1:' + str(server.server_address[1]), 'token_file': str(self.root / 'pc.token')}
        config = dict(self.config, hub=dict(endpoint, token_file=str(self.root / 'mac.token')))
        atomic_json(self.config_path, config)
        worker = ResourceWorker(self.config_path)
        try:
            worker.announce()
            posted = request_json(endpoint, '/v1/resource-operations', {'request_id': 'http-write', 'resource_id': self.resource,
                'operation': 'write', 'arguments': {'path': 'posts/article.md', 'content': '真实 HTTP 操作', 'expected_sha256': 'absent'}})
            with patch.object(worker, 'report', side_effect=NetworkError('lost reply', 502)):
                with self.assertRaises(NetworkError):
                    worker.once()
            self.assertEqual((self.site / 'posts/article.md').read_text(encoding='utf-8'), '真实 HTTP 操作')
            # Another local edit proves recovery submits the receipt and does not repeat the write.
            (self.site / 'posts/article.md').write_text('preserve later edit', encoding='utf-8')
            ResourceWorker(self.config_path).recover()
            result = request_json(endpoint, '/v1/resource-operations/' + posted['id'])
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual((self.site / 'posts/article.md').read_text(encoding='utf-8'), 'preserve later edit')
            # This test uses two authenticated identities on this machine, not a Windows OS claim.
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_result_retrieval_requires_requester_and_exact_terminal_result(self):
        job = self.execute()
        self.assertNotIn('retrieval', job)
        body = {'result_sha256': digest([job['status'], job['result']])}
        with self.assertRaises(NetworkError) as denied:
            self.r.retrieved(self.mac, job['id'], body)
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(NetworkError):
            self.r.retrieved(self.pc, job['id'], {'result_sha256': '0' * 64})
        received = self.r.retrieved(self.pc, job['id'], body)
        self.assertEqual(received['retrieval']['device_id'], 'pc')
        self.assertEqual(self.r.retrieved(self.pc, job['id'], body)['retrieval'], received['retrieval'])
        queued = self.submit(key='not-finished')
        with self.assertRaises(NetworkError):
            self.r.retrieved(self.pc, queued['id'], {'result_sha256': digest(['queued', None])})

    def test_recovered_result_needs_its_own_retrieval_confirmation(self):
        self.submit()
        job = self.r.claim(self.mac, {'runner_id': 'run'})['operation']
        body = {'claim_token': job['claim_token'], 'status': 'uncertain', 'result': {'error': 'lost connection'}}
        uncertain = self.r.receipt(self.mac, job['id'], 'receipt', body)
        old = {'result_sha256': uncertain['result_sha256']}
        self.r.retrieved(self.pc, job['id'], old)
        recovered = self.r.receipt(self.mac, job['id'], 'receipt', dict(body, status='succeeded', result={'actual': True}))
        self.assertNotIn('retrieval', recovered)
        with self.assertRaises(NetworkError):
            self.r.retrieved(self.pc, job['id'], old)
        self.assertIn('retrieval', self.r.retrieved(self.pc, job['id'], {'result_sha256': recovered['result_sha256']}))

    def test_duplicate_worker_waits_then_takes_over_released_lock(self):
        worker = ResourceWorker(self.config_path)
        path = worker.journal.parent / 'worker.lock'
        owner = InstanceLock(path, purpose='test-owner')
        self.assertTrue(owner.acquire())
        entered = threading.Event()
        def run_owned():
            entered.set()
            worker.stop.wait(5)
        with patch.object(worker, '_run_locked', side_effect=run_owned):
            thread = threading.Thread(target=worker.run); thread.start()
            try:
                self.assertFalse(entered.wait(.2))
                self.assertTrue(thread.is_alive())
                self.assertFalse(worker.owned.is_set())
                owner.close()
                self.assertTrue(entered.wait(3))
                self.assertTrue(worker.owned.is_set())
                self.assertTrue(inspect_lock(path)['running'])
            finally:
                owner.close(); worker.stop.set(); thread.join(timeout=3)
        self.assertFalse(inspect_lock(path)['running'])
        self.assertTrue(path.exists())  # Never unlink and split the lock inode.

    def test_device_tracks_overlapping_clients_without_flagging_clean_restart(self):
        body = {'name': 'device', 'environment_id': 'native', 'client_version': '0.3.0'}
        self.c.device_heartbeat(self.mac, body)
        self.c.device_heartbeat(self.mac, dict(body, client_version='0.4.0'))
        device = next(d for d in self.c.devices(self.mac)['devices'] if d['id'] == 'mac')
        self.assertFalse(device['connection_conflict'])
        self.c.device_heartbeat(self.mac, body)
        device = next(d for d in self.c.devices(self.mac)['devices'] if d['id'] == 'mac')
        self.assertTrue(device['connection_conflict'])
        self.assertEqual({c['version'] for c in device['connections']}, {'0.3.0', '0.4.0'})
        with self.hub.store.connect() as db:
            saved = self.c.get(db, 'device', 'mac')
            for connection in saved['connections']:
                if connection['version'] == '0.3.0':
                    connection['last_seen'] = '2000-01-01T00:00:00+00:00'
            self.c.put(db, 'device', saved)
        device = next(d for d in self.c.devices(self.mac)['devices'] if d['id'] == 'mac')
        self.assertFalse(device['connection_conflict'])

    def test_supervisor_single_instance_and_parent_loss_stops_device(self):
        server = make_server(self.config, ('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cfg = dict(self.config, role='client', execution_enabled=False,
                   hub=dict(self.config['hub'], url='http://127.0.0.1:' + str(server.server_address[1])))
        atomic_json(self.config_path, cfg)
        argv = [sys.executable, 'agent_board.py', 'network', '--config', str(self.config_path), 'service']
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        runtime = Path(cfg['runtime_dir'])
        lock = runtime / 'resource-operations/worker.lock'
        try:
            self.assertIn('started', process.stdout.readline())
            deadline = time.monotonic() + 8
            while not inspect_lock(lock)['running'] and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(inspect_lock(lock)['running'])
            duplicate = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=8)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn('another local service', duplicate.stderr)
            process.kill(); process.wait(timeout=5)  # No Python signal handler runs.
            deadline = time.monotonic() + 8
            while inspect_lock(lock)['running'] and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(inspect_lock(lock)['running'])
            self.assertFalse(inspect_lock(runtime / 'service.lock')['running'])
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_real_http_probe_records_requester_receipt_without_starting_a_chat(self):
        server = make_server(self.config, ('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cfg = dict(self.config, hub=dict(self.config['hub'], url='http://127.0.0.1:' + str(server.server_address[1])))
        atomic_json(self.config_path, cfg)
        caller = dict(cfg, hub=dict(cfg['hub'], token_file=str(self.root / 'pc.token')),
                      worker={'device_id': 'pc'})
        worker = ResourceWorker(self.config_path)
        execution = threading.Thread(target=worker.run); execution.start()
        before = self.c.list_agents(self.mac, 'p')
        try:
            report = check_ecosystem(caller, 'MyWeb', 'check', 'stable-probe')
            self.assertTrue(report['passed']); self.assertTrue(report['cross_device'])
            self.assertEqual(report['requester_device'], 'pc')
            self.assertEqual(len(report['operations']), 2)
            self.assertEqual(report['operations'][1]['result']['exit_code'], 0)
            self.assertNotIn(self.secret.read_text(encoding='utf-8'), json.dumps(report))
            self.assertTrue(all(o['retrieval']['device_id'] == 'pc' for o in report['operations']))
            again = check_ecosystem(caller, 'MyWeb', 'check', 'stable-probe')
            self.assertEqual([o['id'] for o in report['operations']], [o['id'] for o in again['operations']])
            self.assertEqual(self.c.list_agents(self.mac, 'p'), before)
        finally:
            worker.stop.set(); execution.join(timeout=5)
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
