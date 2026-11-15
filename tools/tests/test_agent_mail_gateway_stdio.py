import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_mail_gateway_stdio as transport


class PrivateStdioTests(unittest.TestCase):
    def test_request_body_is_only_on_stdin(self):
        secret='synthetic-private-mail-never-in-argv'
        cfg=SimpleNamespace(node_path='/node',build_root='/build',config_path='/pinned-config')
        captured={}
        def run(argv,**kwargs):
            captured['argv']=argv;captured['input']=kwargs['input']
            return SimpleNamespace(returncode=0,stdout=b'{"status":"ok","value":{"messageId":"x"}}')
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.subprocess,'run',side_effect=run):
            out=transport.call(cfg,'/helper','a'*64,'chat.inject',{'message':secret})
        self.assertEqual(out['messageId'],'x');self.assertNotIn(secret,' '.join(captured['argv']))
        self.assertIn(secret,json.loads(captured['input'])['params']['message'])

    def test_only_two_methods_and_changed_helper_are_rejected(self):
        with self.assertRaises(ValueError): transport.call(None,'/helper','a'*64,'agent',{})
        cfg=SimpleNamespace(node_path='/node',build_root='/build',config_path='/pinned-config')
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('b'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.subprocess,'run') as run:
            with self.assertRaises(ValueError): transport.call(cfg,'/helper','a'*64,'chat.inject',{})
            run.assert_not_called()

    def test_timeout_is_unknown_to_caller_no_retry(self):
        cfg=SimpleNamespace(node_path='/node',build_root='/build',config_path='/pinned-config')
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), \
             patch.object(transport.subprocess,'run',side_effect=subprocess.TimeoutExpired('node',30)) as run:
            with self.assertRaisesRegex(ValueError,'gateway_unavailable'): transport.call(cfg,'/helper','a'*64,'chat.inject',{})
            self.assertEqual(run.call_count,1)

    def test_config_change_after_ack_is_not_retried(self):
        cfg=SimpleNamespace(node_path='/node',build_root='/build',config_path='/pinned-config')
        response=SimpleNamespace(returncode=0,stdout=b'{"status":"ok","value":{"messageId":"receipt"}}')
        with patch.object(transport.host,'_preflight',side_effect=[None,transport.host._Changed()]), \
             patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), \
             patch.object(transport.subprocess,'run',return_value=response) as run:
            with self.assertRaises(transport.host._Changed): transport.call(cfg,'/helper','a'*64,'chat.inject',{})
            self.assertEqual(run.call_count,1)

    @unittest.skipUnless(os.environ.get('MAIL_ROUTER_PROCESS_TESTS') == '1', 'opt-in subprocess test; strict gate forbids subprocess')
    def test_real_helper_uses_stdin_and_sdk_contract(self):
        node=shutil.which('node')
        if not node: self.skipTest('node unavailable')
        helper=Path(__file__).resolve().parents[2]/'adapters/gateway-private-stdio.mjs'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); sdk=root/'dist/plugin-sdk';sdk.mkdir(parents=True)
            (root/'package.json').write_text('{"type":"module"}')
            (sdk/'gateway-runtime.js').write_text("export async function callGatewayFromCli(m,o,p,x){if(m!=='chat.inject'||o.timeout!=='20000'||x.sharedStateMode!=='read-only'||p.message!=='synthetic')throw Error('invalid');return {messageId:'receipt'};}")
            proc=subprocess.run([node,str(helper),str(root)],input=json.dumps({'method':'chat.inject','params':{'message':'synthetic'}}).encode(),capture_output=True,timeout=10,check=True)
            self.assertEqual(json.loads(proc.stdout),{'status':'ok','value':{'messageId':'receipt'}})
            self.assertEqual(proc.stderr,b'')
            bad=subprocess.run([node,str(helper),str(root)],input=b'{"method":"agent","params":{}}',capture_output=True,timeout=10,check=True)
            self.assertEqual(json.loads(bad.stdout),{'status':'error','error':'gateway_unavailable'})

if __name__=='__main__':unittest.main()
