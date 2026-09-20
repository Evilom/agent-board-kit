"""Real HTTP checks for existing-task inbox inspection and receipt boundaries."""

import os
import threading
import unittest
from unittest.mock import patch

import test_collaboration as base
from board_network.client import AgentClient
from board_network.common import NetworkError
from board_network.hub import make_server
from board_network.session_inbox import check_session


class SessionInboxTests(unittest.TestCase):
    actor = base.CollaborationTests.actor
    register = base.CollaborationTests.register
    work = base.CollaborationTests.work

    def setUp(self):
        base.CollaborationTests.setUp(self)
        self.server = make_server(self.config, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config['hub'] = {
            'url': 'http://127.0.0.1:' + str(self.server.server_address[1]),
            'token_file': str(self.root / 'mac.token'),
        }
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': 'native-existing-task'})
        self.env.start()
        self.w = self.work()
        self.incoming = self.c.send_message(self.pc, {
            'project_id': 'p', 'request_id': 'incoming', 'work_id': self.w['id'],
            'to_agent_id': self.a['id'], 'from_agent_id': self.b['id'],
            'session_id': 's1', 'body': 'Result evidence, not a new user instruction.',
        })

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        base.CollaborationTests.tearDown(self)

    def inspect(self, **overrides):
        args = dict(config=self.config, project='p', agent_id=self.a['id'],
                    session_id='s1', native_thread_id='native-existing-task',
                    work_ids=[self.w['id']])
        args.update(overrides)
        return check_session(**args)

    def saved(self, message):
        with self.c.store.connect() as db:
            return self.c.get(db, 'message', message['id'])

    def test_repeated_checks_preserve_agents_work_and_ack(self):
        before_agents = self.c.list_agents(self.mac, 'p')
        before_work = self.c.work_detail(self.mac, self.w['id'])
        with patch.object(AgentClient, 'connect', side_effect=AssertionError('new agent')), \
                patch('subprocess.Popen', side_effect=AssertionError('new process')):
            first, second = self.inspect(), self.inspect()
        self.assertEqual(first['messages'], second['messages'])
        self.assertEqual(first['work'][0]['constraints'], self.w['constraints'])
        self.assertEqual(first['work'][0]['acceptance'], self.w['acceptance'])
        self.assertIsNone(self.saved(self.incoming)['acknowledged_at'])
        self.assertTrue(self.saved(self.incoming)['delivered_at'])
        self.assertEqual(before_work, self.c.work_detail(self.mac, self.w['id']))
        self.assertEqual(before_agents, self.c.list_agents(self.mac, 'p'))

    def test_wrong_or_missing_native_task_never_contacts_board(self):
        with patch.object(AgentClient, 'call') as call:
            for native_id in ('another-task', ''):
                with patch.dict(os.environ, {'CODEX_THREAD_ID': native_id}):
                    with self.assertRaises(NetworkError):
                        self.inspect()
            call.assert_not_called()
        self.assertIsNone(self.saved(self.incoming)['delivered_at'])

    def test_stale_session_and_other_device_cannot_read_inbox(self):
        for args in ({'session_id': 'stale'}, {'agent_id': self.b['id']}):
            with self.assertRaises(NetworkError):
                self.inspect(**args)
        self.assertIsNone(self.saved(self.incoming)['delivered_at'])

    def test_scope_is_explicit_and_unrelated_messages_remain_unacknowledged(self):
        unrelated = self.c.send_message(self.pc, {
            'project_id': 'p', 'request_id': 'unrelated',
            'to_agent_id': self.a['id'], 'body': 'Unrelated old request',
        })
        result = self.inspect()
        self.assertEqual([m['id'] for m in result['messages']], [self.incoming['id']])
        self.assertEqual([m['id'] for m in result['deferred']], [unrelated['id']])
        self.assertNotIn('body', result['deferred'][0])
        self.assertIsNone(self.saved(unrelated)['acknowledged_at'])
        with self.assertRaises(NetworkError):
            self.inspect(work_ids=[])

    def test_receipt_requires_real_recipient_ack_and_does_not_mark_peer_delivered(self):
        outgoing = self.c.send_message(self.mac, {
            'project_id': 'p', 'request_id': 'outgoing', 'work_id': self.w['id'],
            'to_agent_id': self.b['id'], 'from_agent_id': self.a['id'],
            'session_id': 's1', 'body': 'Published result',
        })
        ids = [outgoing['id'], 'msg-absent', self.incoming['id']]
        receipts = self.inspect(receipt_ids=ids)['receipts']
        self.assertEqual([r['status'] for r in receipts], ['pending', 'unverified', 'unverified'])
        self.assertIsNone(self.saved(outgoing)['delivered_at'])
        self.c.ack_message(self.pc, outgoing['id'], {'agent_id': self.b['id'], 'session_id': 's1'})
        receipt = self.inspect(receipt_ids=[outgoing['id']])['receipts'][0]
        self.assertEqual(receipt['status'], 'acknowledged')
        self.assertEqual(receipt['acknowledged_at'], self.saved(outgoing)['acknowledged_at'])
        self.assertIsNone(self.saved(self.incoming)['acknowledged_at'])


if __name__ == '__main__':
    unittest.main()
