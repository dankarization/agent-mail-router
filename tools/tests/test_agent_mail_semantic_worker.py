import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_mail_router as router
import agent_mail_semantic_worker as worker
from test_agent_mail_router import ROOT
from test_agent_mail_gateway_catalog import CatalogTests


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = CatalogTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.routes = ROOT / 'examples/routes.example.json'
        store = router.Store(self.root / 'mail.sqlite3', router.routes_at(self.routes))
        with store.db:
            store.db.execute("INSERT INTO effects VALUES ('x','s','context','t','inflight',NULL,NULL)")
        store.db.close()
        fields = {name:'x' for name in worker.TrustedHostConfig.__dataclass_fields__}
        fields.update(policy_path=str(self.fixture.path), agent_id='main', allowed_agents=['main'], wall_timeout_ms=120000)
        self.config = self.root / 'host.json'; self.config.write_text(json.dumps(fields)); self.config.chmod(0o600)
        self.argv = ['--state-dir',str(self.root),'--routes',str(self.routes),
                     '--scope',str(self.fixture.path),'--host-config',str(self.config),
                     '--gateway-helper','/synthetic/helper','--gateway-helper-sha256','a'*64]

    def test_empty_queue_does_not_touch_other_inflight_effects(self):
        out=io.StringIO()
        with patch.object(router.Gateway, 'call', side_effect=AssertionError('no RPC')), \
             patch.object(worker, 'classify', side_effect=AssertionError('no model')), contextlib.redirect_stdout(out):
            self.assertEqual(worker.main(self.argv), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'idle')
        import sqlite3
        with sqlite3.connect(self.root / 'mail.sqlite3') as db:
            self.assertEqual(db.execute("SELECT status FROM effects WHERE id='x'").fetchone()[0], 'inflight')

    def test_untrusted_host_config_and_scope_mismatch_fail_closed(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError): worker.private_host_config(self.config,self.fixture.path)
        self.config.chmod(0o600)
        data=json.loads(self.config.read_text()); data['policy_path']=str(self.root / 'wrong')
        self.config.write_text(json.dumps(data))
        with self.assertRaises((ValueError,FileNotFoundError)): worker.private_host_config(self.config,self.fixture.path)

    def test_second_worker_does_not_wait_or_invoke_model(self):
        out=io.StringIO()
        with (self.root / 'semantic-worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(worker,'classify',side_effect=AssertionError('no model')), contextlib.redirect_stdout(out):
                self.assertEqual(worker.main(self.argv),0)
        self.assertEqual(json.loads(out.getvalue())['reason'],'worker_already_running')

    def test_status_reports_held_and_processing_without_mail_data(self):
        import sqlite3
        secret='private-mail-key-not-for-stdout'
        with sqlite3.connect(self.root/'mail.sqlite3') as db:
            for name,record in [('a',{'version':1,'status':'processing','mailKey':secret}),
                                ('b',{'version':1,'status':'held','mailKey':secret,'outcome':'content_unavailable'}),
                                ('c',{'version':1,'status':'queued','mailKey':secret})]:
                db.execute('INSERT INTO meta(key,value) VALUES (?,?)',('semantic_queue_v1:'+name,json.dumps(record)))
        out=io.StringIO()
        with patch.object(worker,'classify',side_effect=AssertionError('no model')), contextlib.redirect_stdout(out):
            self.assertEqual(worker.main(['--state-dir',str(self.root),'--status']),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['counts']['processing'],1);self.assertEqual(result['counts']['held'],1)
        self.assertEqual(result['heldReasons'],{'content_unavailable':1});self.assertNotIn(secret,out.getvalue())
        self.assertFalse(result['workerActive']); self.assertTrue(result['interruptedProcessing'])

    def test_status_active_lock_does_not_label_processing_interrupted(self):
        import sqlite3
        with sqlite3.connect(self.root/'mail.sqlite3') as db:
            db.execute('INSERT INTO meta(key,value) VALUES (?,?)',('semantic_queue_v1:x',json.dumps({'version':1,'status':'processing'})))
        out=io.StringIO()
        with (self.root/'semantic-worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(out):
                self.assertEqual(worker.main(['--state-dir',str(self.root),'--status']),0)
        result=json.loads(out.getvalue())
        self.assertTrue(result['workerActive']); self.assertFalse(result['interruptedProcessing'])

if __name__ == '__main__': unittest.main()
