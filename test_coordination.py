"""Capabilities, project bulletins and passive notifications across device identities."""
import copy
import unittest

import test_collaboration as base
from board_network import coordination as co
from board_network.common import NetworkError
from board_network.hub import Hub
from board_network.runtime import commands


class CoordinationTests(unittest.TestCase):
    setUp = base.CollaborationTests.setUp
    tearDown = base.CollaborationTests.tearDown
    actor = base.CollaborationTests.actor
    register = base.CollaborationTests.register
    work = base.CollaborationTests.work

    def bulletin(self, request='notice'):
        return co.publish(self.c, self.mac, dict(project_id='p', request_id=request,
            title='Mac 的资料查询能力', body='可以提供 docs 中的原文和版本依据。', category='capability',
            from_agent_id=self.a['id'], session_id='s1'))

    def test_profiles_survive_reconnect_and_match_declared_skills(self):
        p=dict(summary='Python 项目资料', skills=['Python', '资料查询'], tools=['Codex'], knowledge=['docs'], limitations=['只读'])
        a=co.update_profile(self.c,self.mac,self.a['id'],{'session_id':'s1','profile':p})
        self.assertEqual(co.candidates(self.c,self.pc,'p',['python'])['agents'][0]['id'],a['id'])
        self.assertEqual(co.candidates(self.c,self.pc,'p',['Rust'])['agents'],[])
        again=self.c.register_agent(self.mac,dict(project_id='p',workspace_id='mac',agent_id='a',session_id='s1',profile={}))
        self.assertEqual(again['profile'],p);self.assertEqual(again['profile_updated_at'],a['profile_updated_at'])
        co.update_profile(self.c,self.mac,self.a['id'],{'session_id':'s1','profile':{}})
        self.assertEqual(self.register(self.mac,'a')['profile']['skills'],[])

    def test_profile_and_discovery_authorization(self):
        for actor, session in [(self.pc,'s1'),(self.mac,'stale')]:
            with self.assertRaises(NetworkError):co.update_profile(self.c,actor,self.a['id'],{'session_id':session,'profile':{}})
        with self.assertRaises(NetworkError):co.candidates(self.c,self.mac,'private',[])
        with self.assertRaises(NetworkError):co.profile({'secret':'bad'})

    def test_bulletin_idempotency_persistence_and_no_work_creation(self):
        before=self.c.list_work(self.mac,'p')
        item=self.bulletin();self.assertEqual(item,self.bulletin())
        reopened=Hub(self.config).collaboration
        self.assertEqual(co.list_bulletins(reopened,self.pc,'p')['bulletins'][0]['id'],item['id'])
        self.assertEqual(before,self.c.list_work(self.mac,'p'))
        body=dict(project_id='p',request_id='notice',title='不同内容',body='changed')
        with self.assertRaises(NetworkError):co.publish(self.c,self.mac,body)

    def test_announcement_acl_and_sender_fence(self):
        item=self.bulletin()
        with self.assertRaises(NetworkError):co.list_bulletins(self.c,self.pc,'private')
        for actor,fields in [(self.reader,{}),(self.pc,{'from_agent_id':self.a['id'],'session_id':'s1'}),
                             (self.mac,{'from_agent_id':self.a['id'],'session_id':'stale'})]:
            with self.assertRaises(NetworkError):co.publish(self.c,actor,dict(project_id='p',request_id='bad',title='标题',body='内容',**fields))
        with self.assertRaises(NetworkError):co.mark_read(self.c,self.pc,item['id'],{'agent_id':self.a['id'],'session_id':'s1'})
        self.assertEqual(len(co.list_bulletins(self.c,self.reader,'p')['bulletins']),1)

    def test_notifications_do_not_deliver_ack_or_change_task(self):
        item=self.bulletin();work=self.work()
        m=self.c.send_message(self.mac,dict(project_id='p',request_id='private',to_agent_id=self.b['id'],body='请查看公告'))
        for _ in range(2):
            update=co.updates(self.c,self.pc,self.b['id'],'s1')
            self.assertEqual(update['unread_bulletins'],1);self.assertEqual(update['messages'][0]['id'],m['id'])
            co.list_bulletins(self.c,self.pc,'p',self.b['id'],'s1')
        with self.c.store.connect() as db:
            saved=self.c.get(db,'message',m['id']);self.assertIsNone(saved['delivered_at']);self.assertIsNone(saved['acknowledged_at'])
        self.assertEqual(self.c.work_detail(self.mac,work['id'])['revision'],1)
        self.assertEqual(co.updates(self.c,self.mac,self.a['id'],'s1')['messages'],[])
        with self.assertRaises(NetworkError):co.updates(self.c,self.mac,self.b['id'],'s1')
        with self.assertRaises(NetworkError):co.updates(self.c,self.pc,self.b['id'],'stale')

    def test_read_receipts_are_separate_for_browser_and_each_agent(self):
        item=self.bulletin()
        own={'agent_id':self.b['id'],'session_id':'s1'}
        r=co.mark_read(self.c,self.pc,item['id'],own)
        self.assertEqual(r,co.mark_read(self.c,self.pc,item['id'],own))
        self.assertEqual(co.updates(self.c,self.pc,self.b['id'],'s1')['unread_bulletins'],0)
        self.assertEqual(co.list_bulletins(self.c,self.pc,'p')['unread_count'],1)
        self.assertEqual(co.updates(self.c,self.mac,self.a['id'],'s1')['unread_bulletins'],1)
        co.mark_read(self.c,self.pc,item['id'],{})
        self.assertEqual(co.list_bulletins(self.c,self.pc,'p')['unread_count'],0)
        self.assertEqual(co.list_bulletins(self.c,self.pc,'p',self.b['id'],'s1',unread=True)['bulletins'],[])

    def test_older_unread_not_lost_after_500_newer_records(self):
        first=self.bulletin('first')
        # Populate history without generating unrelated notification messages.
        with self.c.store.connect() as db:
            for i in range(505):self.c.put(db,'bulletin',dict(first,id='bulletin-'+str(i)))
        seen=[];cursor=None
        while True:
            page=co.list_bulletins(self.c,self.pc,'p',self.b['id'],'s1',before=cursor,unread=True)
            seen.extend(b['id'] for b in page['bulletins']);cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(set(seen)),506);self.assertIn(first['id'],seen)
        self.assertEqual(co.updates(self.c,self.pc,self.b['id'],'s1')['unread_bulletins'],506)
        with self.assertRaises(NetworkError):co.list_bulletins(self.c,self.pc,'p',before='invalid')

    def test_service_does_not_launch_agents_even_with_old_responder_config(self):
        config=copy.deepcopy(self.config);config.update(role='client',execution_enabled=False,runtime_dir=str(self.root),responders=[{'enabled':True,'name':'old-auto','provider':'codex'}])
        jobs=commands(config,self.root/'client.json')
        self.assertEqual([name for name,_ in jobs],['device'])
        self.assertFalse(any('codex' in arg or 'respond' in arg for _,cmd in jobs for arg in cmd))
