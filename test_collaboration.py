"""Portable Agent Board: ownership, message privacy, browser auth and MCP contracts."""
import base64
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from board_network.common import NetworkError, request_json
from board_network.hub import Hub, make_server
from board_network.client import AgentClient
from board_network import managed
from test_board_network import FakeDagu


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {'version': 1, 'role': 'server', 'database': str(self.root / 'board.sqlite3'),
                       'dagu': {'url': 'http://127.0.0.1:8188'}, 'knowledge_sources': {},
                       'projects': {'p': {'workspaces': {}}, 'private': {'workspaces': {}}}, 'principals': {}}
        for index, name in enumerate(('mac', 'pc', 'reader')):
            token = self.root / (name + '.token');token.write_text(chr(97 + index) * 32)
            self.config['principals'][name] = {'device_id': name, 'token_file': str(token), 'grants': {
                'p': {'operations': ['read'] if name == 'reader' else ['read', 'query', 'collaborate', 'execute', 'accept'], 'workspaces': [name]}}}
            self.config['projects']['p']['workspaces'][name] = {'device_id': name, 'environment_id': name + '-native', 'os': 'Darwin', 'root': str(self.root), 'python': sys.executable}
        self.hub = Hub(self.config);self.c = self.hub.collaboration
        self.mac = self.actor('mac');self.pc = self.actor('pc');self.reader = self.actor('reader')
        self.a = self.register(self.mac, 'a');self.b = self.register(self.pc, 'b')

    def tearDown(self):
        self.tmp.cleanup()

    def actor(self, name):
        return self.hub.authenticate('Bearer ' + Path(self.config['principals'][name]['token_file']).read_text())

    def register(self, actor, name, session='s1'):
        return self.c.register_agent(actor, dict(project_id='p', workspace_id=actor[0], agent_id=name, session_id=session, provider='test'))

    def work(self, request='create'):
        return self.c.create_work(self.mac, {'project_id':'p','request_id':request,'title':'跨设备分析','goal':'整理项目资料', 'scope':['.'], 'constraints':['不改原文'], 'acceptance':['有依据']})

    def change(self, actor, agent, work, action, **body):
        return self.c.transition(actor, work['id'], action, dict(agent_id=agent['id'],session_id=agent['session_id'],attempt_id=work['attempt_id'],revision=work['revision'],**body))

    def test_claim_race_and_stale_handoff_fence(self):
        work = self.work()
        def claim(pair):
            try:return self.change(*pair,work,'claim')
            except NetworkError as exc:return exc.status
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(claim, [(self.mac,self.a),(self.pc,self.b)]))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1);self.assertIn(409,results)
        work=next(r for r in results if isinstance(r,dict))
        first, second=(self.a,self.b) if work['owner']['agent_id']==self.a['id'] else (self.b,self.a)
        actor1, actor2=(self.mac,self.pc) if first==self.a else (self.pc,self.mac)
        old=copy.deepcopy(work)
        work=self.change(actor1,first,work,'handoff',to_agent_id=second['id'],completed='已读资料',remaining='整理结论',context='来源在 docs',released=True)
        work=self.change(actor2,second,work,'handoff-accept')
        self.assertEqual(work['id'],old['id']);self.assertEqual(work['constraints'],old['constraints']);self.assertNotEqual(work['attempt_id'],old['attempt_id'])
        old['revision']=work['revision']
        with self.assertRaises(NetworkError):self.change(actor1,first,old,'progress',note='旧执行覆盖')
        work=self.change(actor2,second,work,'result',summary='已完成',checks=[{'status':'passed','evidence':'读取实际来源'}],artifact_ids=[])
        done=self.c.transition(self.mac,work['id'],'accept',{'revision':work['revision'],'note':'已核验'})
        self.assertEqual(done['status'],'done')
        reopened=Hub(self.config).collaboration.work_detail(self.mac,done['id'])
        self.assertEqual(reopened['status'],'done');self.assertEqual(len(reopened['attempts']),2)

    def test_idempotency_and_result_retry(self):
        w=self.work();self.assertEqual(w,self.work())
        w=self.change(self.mac,self.a,w,'claim')
        body=dict(agent_id=self.a['id'],session_id='s1',attempt_id=w['attempt_id'],revision=w['revision'],summary='分析完成',checks=[{'status':'unverified','evidence':'需要人工检查'}],artifact_ids=[])
        one=self.c.transition(self.mac,w['id'],'result',body)
        two=self.c.transition(self.mac,w['id'],'result',body)
        self.assertEqual(one,two)
        with self.assertRaises(NetworkError):self.c.transition(self.mac,w['id'],'accept',{'revision':one['revision'],'note':'跳过验证'})

    def test_message_privacy_and_ack(self):
        data={'project_id':'p','request_id':'msg1','to_agent_id':self.b['id'],'body':'只给接收方','from_agent_id':self.a['id'],'session_id':'s1'}
        message=self.c.send_message(self.mac,data)
        self.assertEqual(message,self.c.send_message(self.mac,data))
        self.assertEqual(self.c.messages(self.reader,'p')['messages'],[])
        with self.assertRaises(NetworkError):self.c.messages(self.mac,'p',self.b['id'],'s1')
        inbox=self.c.messages(self.pc,'p',self.b['id'],'s1')['messages'];self.assertEqual(len(inbox),1);self.assertTrue(inbox[0]['delivered_at'])
        with self.assertRaises(NetworkError):self.c.ack_message(self.mac,message['id'],{'agent_id':self.a['id'],'session_id':'s1'})
        self.c.ack_message(self.pc,message['id'],{'agent_id':self.b['id'],'session_id':'s1'})
        self.assertEqual(self.c.messages(self.pc,'p',self.b['id'],'s1')['messages'],[])

    def test_artifacts_require_current_attempt_and_hash(self):
        w=self.change(self.mac,self.a,self.work(),'claim');raw='真实产物'.encode()
        body=dict(agent_id=self.a['id'],session_id='s1',attempt_id=w['attempt_id'],name='report.txt',content=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
        item=self.c.upload(self.mac,w['id'],body);self.assertEqual(item['size'],len(raw))
        for key,value in [('sha256','a'*64),('name','../report'),('attempt_id','old')]:
            with self.assertRaises(NetworkError):self.c.upload(self.mac,w['id'],dict(body,**{key:value}))
        with self.assertRaises(NetworkError):self.c.upload(self.pc,w['id'],dict(body,agent_id=self.b['id']))

    def test_session_restart_does_not_steal_ownership(self):
        w=self.change(self.mac,self.a,self.work(),'claim')
        self.c.heartbeat_agent(self.mac,self.a['id'],{'session_id':'s1','state':'offline'})
        with self.assertRaises(NetworkError):self.register(self.mac,'a','new-session')
        restored=self.register(self.mac,'a','s1')
        w=self.change(self.mac,restored,w,'progress',note='已恢复连接')
        self.assertEqual(w['status'],'active')

    def test_read_only_principal_cannot_join_or_change(self):
        with self.assertRaises(NetworkError):self.register(self.reader,'steal')
        w=self.work()
        with self.assertRaises(NetworkError):self.c.transition(self.reader,w['id'],'assign',{'revision':1,'target_agent_id':self.a['id']})
        with self.assertRaises(NetworkError):self.c.list_work(self.mac,'private')

    def test_managed_dispatch_lost_response_not_repeated(self):
        fake=FakeDagu();fake.drop_ack=True;fake.offline=False;self.hub.dagu=fake
        w=self.work();body={'revision':w['revision'],'workspace_id':'mac','capability':'environment.info/v1'}
        # Keep reconciliation unavailable so the uncertain external effect remains visible.
        def unavailable(task):raise NetworkError('网络中断',502)
        fake.evidence=unavailable
        one=managed.run(self.c,self.mac,w['id'],body)
        two=managed.run(self.c,self.mac,w['id'],body)
        self.assertEqual(fake.calls,1);self.assertEqual(one['execution_id'],two['execution_id'])
        self.assertEqual(two['execution']['status'],'unknown')

    def test_managed_completed_review_and_explicit_rerun(self):
        fake=FakeDagu();self.hub.dagu=fake
        w=self.work();w=managed.run(self.c,self.mac,w['id'],{'revision':w['revision'],'workspace_id':'mac','capability':'environment.info/v1'})
        self.assertEqual(w['status'],'review');self.assertEqual(w['result']['checks'][0]['status'],'unverified')
        w=self.c.transition(self.mac,w['id'],'reopen',{'revision':w['revision'],'note':'补充检查'})
        self.assertEqual(w['status'],'ready')
        w=managed.run(self.c,self.mac,w['id'],{'revision':w['revision'],'workspace_id':'mac','capability':'workspace.inventory/v1'})
        self.assertEqual(fake.calls,2);self.assertEqual(len(w['attempts']),2)

    def test_http_cookie_csrf_and_mcp_two_processes(self):
        server=make_server(self.config,('127.0.0.1',0));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_address[1]);processes=[]
        try:
            endpoint={'url':url,'token_file':str(self.root/'mac.token')}
            ticket=request_json(endpoint,'/v1/browser-ticket',{})['ticket']
            req=Request(url+'/v1/browser-session',data=json.dumps({'ticket':ticket}).encode(),headers={'Origin':url,'Content-Type':'application/json'})
            with urlopen(req) as r:cookie=r.headers['Set-Cookie'].split(';')[0];self.assertIn('HttpOnly',r.headers['Set-Cookie'])
            with urlopen(Request(url+'/v1/me',headers={'Cookie':cookie})) as r:self.assertEqual(json.load(r)['principal_id'],'mac')
            with self.assertRaises(HTTPError) as caught:urlopen(Request(url+'/v1/logout',data=b'{}',headers={'Cookie':cookie,'Origin':'http://evil.invalid'}))
            self.assertEqual(caught.exception.code,403);caught.exception.close()
            with self.assertRaises(HTTPError) as caught:urlopen(req)
            self.assertEqual(caught.exception.code,401);caught.exception.close()
            with urlopen(url+'/') as r:self.assertIn('协作空间',r.read().decode())
            def rpc(p,i,method,params={}):
                p.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\n');p.stdin.flush();return json.loads(p.stdout.readline())['result']
            def call(p,i,name,args={}):
                result=rpc(p,i,'tools/call',{'name':name,'arguments':args});self.assertFalse(result.get('isError'),result);return json.loads(result['content'][0]['text'])
            for device in ('mac','pc'):
                cfg=self.root/(device+'.json');cfg.write_text(json.dumps(dict(self.config,hub={'url':url,'token_file':str(self.root/(device+'.token'))})))
                p=subprocess.Popen([sys.executable,'-m','board_network.cli','--config',str(cfg),'mcp','--project','p','--workspace',device,'--name','live-'+device],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
                processes.append(p);init=rpc(p,1,'initialize',{'protocolVersion':'2025-06-18'});self.assertIn('tools',init['capabilities'])
                self.assertEqual(len(rpc(p,2,'tools/list')['tools']),15)
            a,b=processes
            aid=call(a,3,'board_status')['self']['id'];bid=call(b,3,'board_status')['self']['id']
            w=call(a,4,'board_create',{'request_id':'mcp-work','title':'双进程验证','goal':'协作交接','scope':['.'],'constraints':['只读'],'acceptance':['完整交接']})
            w=call(a,5,'board_claim',{'work_id':w['id'],'revision':w['revision']})
            call(a,6,'board_message',{'request_id':'mcp-msg','to_agent_id':bid,'body':'请接手','work_id':w['id']})
            msg=call(b,4,'board_inbox')['messages'][0];call(b,5,'board_ack',{'message_id':msg['id']})
            w=call(a,7,'board_handoff',{'work_id':w['id'],'revision':w['revision'],'attempt_id':w['attempt_id'],'to_agent_id':bid,'completed':'初步分析','remaining':'验证','context':'来源已保留','released':True})
            w=call(b,6,'board_receive',{'work_id':w['id'],'revision':w['revision']})
            w=call(b,7,'board_result',{'work_id':w['id'],'revision':w['revision'],'attempt_id':w['attempt_id'],'summary':'交接完成','checks':[{'status':'passed','evidence':'两个独立 MCP 进程'}],'artifact_ids':[]})
            self.assertEqual(w['status'],'review')
        finally:
            for p in processes:
                p.stdin.close();p.wait(timeout=15);p.stdout.close();p.stderr.close()
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
