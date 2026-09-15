"""Portable Agent Board: real filesystem boundaries, upgrade backups and CLI launch contracts."""
import contextlib
import hashlib
import io
import json
import os
import platform
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from board_network.execution import run
from board_network.upgrade import upgrade
from board_network.client import launch
from board_network.runtime import commands


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.workspace=self.root/'plain-folder';self.workspace.mkdir()
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        (self.workspace/'说明.txt').write_text('本地原文',encoding='utf-8')
        (self.workspace/'.runtime').mkdir();(self.workspace/'.runtime'/'secret.token').write_text('do-not-index')
        token=self.root/'owner.token';token.write_text('a'*32)
        self.cfg={'version':1,'role':'server','database':str(self.root/'board.sqlite3'),'runtime_dir':str(self.runtime),
                  'dagu':{'url':'http://127.0.0.1:8188'},'hub':{'url':'http://127.0.0.1:8940','token_file':str(token)},
                  'worker':{'device_id':'local','environment_id':'native'},'projects':{'p':{'workspaces':{'ws':{
                      'device_id':'local','environment_id':'native','os':platform.system(),'root':str(self.workspace),'python':sys.executable}}}},
                  'principals':{'owner':{'device_id':'local','token_file':str(token),'grants':{'p':{'operations':['read','execute','accept'],'workspaces':['ws']}}}},'knowledge_sources':{}}
        self.path=self.root/'config.json';self.path.write_text(json.dumps(self.cfg))
        self.contract={'task_id':'task-test','attempt_id':'attempt-test','project_id':'p','workspace_id':'ws','device_id':'local','environment_id':'native','os':platform.system(),'recipe':'workspace.inventory/v1','scope':['.'],'goal':'读取文件','constraints':['只读'],'acceptance':['来源存在']}

    def tearDown(self):self.tmp.cleanup()

    def execute(self,contract):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=run(contract,str(self.path))
        return code,json.loads(output.getvalue().split('AGENT_BOARD_RECEIPT=',1)[1])

    def test_inventory_non_git_and_boundaries(self):
        before=(self.workspace/'说明.txt').read_bytes()
        code,result=self.execute(self.contract)
        self.assertEqual(code,0)
        self.assertEqual([f['path'] for f in result['result']['files']],['说明.txt'])
        self.assertEqual(result['result']['files'][0]['sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual((self.workspace/'说明.txt').read_bytes(),before)
        self.assertEqual(self.execute(dict(self.contract,scope=['../']))[0],1)
        if os.name!='nt':
            (self.workspace/'outside').symlink_to(self.root)
            self.assertEqual(self.execute(dict(self.contract,scope=['outside']))[0],1)

    def test_execution_identity_and_agent_opt_in(self):
        self.assertEqual(self.execute(dict(self.contract,device_id='other'))[0],1)
        self.assertEqual(self.execute(dict(self.contract,recipe='agent.codex.read/v1'))[0],1)

    def test_upgrade_preserves_records_and_verified_backup(self):
        from board_network.hub import Hub
        hub=Hub(self.cfg)
        with hub.store.connect() as db:
            db.execute("INSERT INTO collab_records VALUES('message','m','p','{\"id\":\"m\",\"body\":\"保留原始消息\"}')")
        result=upgrade(self.path)
        backup=Path(result['database_backup']);self.assertTrue(backup.is_file())
        for path in (backup,Path(self.cfg['database'])):
            with sqlite3.connect(str(path)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertIn('保留原始消息',db.execute('SELECT data FROM collab_records').fetchone()[0])
        self.assertIn('collaborate',json.loads(self.path.read_text())['principals']['owner']['grants']['p']['operations'])
        self.assertNotIn('collaborate',json.loads(Path(result['config_backup']).read_text())['principals']['owner']['grants']['p']['operations'])

    def test_client_without_execution_and_scoped_launch(self):
        jobs=commands(dict(self.cfg,role='client',execution_enabled=False),str(self.path))
        self.assertEqual([name for name,_ in jobs],['device'])
        args=SimpleNamespace(project='p',workspace='ws',name='codex-session',provider='codex',session=None)
        with patch('board_network.client.agent_executable',return_value='/installed/codex'),patch('board_network.client.subprocess.call',return_value=0) as called:
            self.assertEqual(launch(self.cfg,self.path,args),0)
            command=called.call_args.args[0]
            self.assertTrue(any('mcp_servers.agent_board.args=' in x for x in command))
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',command)
            self.assertEqual(called.call_args.kwargs['cwd'],str(self.workspace))

if __name__=='__main__':unittest.main()
