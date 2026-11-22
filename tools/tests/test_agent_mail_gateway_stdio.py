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
    def setUp(self):
        self.cfg=SimpleNamespace(node_path='/node',build_root='/build',config_path='/pinned-config')

    def test_request_body_is_only_on_stdin_and_owned_group_exchange(self):
        secret='synthetic-private-mail-never-in-argv';captured={}
        def exchange(argv,cwd,env,payload,end,**kwargs):
            captured.update(argv=argv,input=payload,env=env,kwargs=kwargs)
            return b'{"messageId":"receipt"}'
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.host,'_exchange',side_effect=exchange):
            out=transport.call(self.cfg,'/helper','a'*64,'chat.inject',{'message':secret})
        self.assertEqual(out['messageId'],'receipt');self.assertNotIn(secret,' '.join(captured['argv']))
        self.assertNotIn(secret,json.dumps(captured['env']))
        self.assertEqual(json.loads(captured['input'])['params']['message'],secret)
        self.assertTrue(captured['kwargs']['_validate_output'](b'{"ok":true}'))
        self.assertFalse(captured['kwargs']['_validate_output'](b'not-json'))

    def test_escaped_12k_body_fits_96k_python_gateway_wire_and_oversize_is_rejected(self):
        captured={}
        def exchange(argv,cwd,env,payload,end,**kwargs):
            captured['payload']=payload
            return b'{"messageId":"receipt"}'
        body='\0'*12000
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.host,'_exchange',side_effect=exchange):
            self.assertEqual(transport.call(self.cfg,'/helper','a'*64,'chat.inject',{'message':body}),{'messageId':'receipt'})
            self.assertGreater(len(captured['payload']),32768)
            self.assertLessEqual(len(captured['payload']),transport.MAX_GATEWAY_INPUT_BYTES)
            self.assertEqual(json.loads(captured['payload'])['params']['message'],body)
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.host,'_exchange') as run:
            with self.assertRaisesRegex(ValueError,'gateway_input_too_large'):
                transport.call(self.cfg,'/helper','a'*64,'chat.inject',{'message':'x'*(96*1024)})
            run.assert_not_called()

    def test_only_two_methods_and_changed_helper_are_rejected(self):
        with self.assertRaises(ValueError): transport.call(None,'/helper','a'*64,'agent',{})
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('b'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), patch.object(transport.host,'_exchange') as run:
            with self.assertRaises(ValueError): transport.call(self.cfg,'/helper','a'*64,'chat.inject',{})
            run.assert_not_called()

    def test_timeout_is_unknown_to_caller_no_retry(self):
        with patch.object(transport.host,'_preflight'), patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), \
             patch.object(transport.host,'_exchange',side_effect=transport.host._CleanupFailed()) as run:
            with self.assertRaisesRegex(ValueError,'gateway_unavailable'): transport.call(self.cfg,'/helper','a'*64,'chat.inject',{})
            self.assertEqual(run.call_count,1)

    def test_config_change_after_ack_is_not_retried(self):
        with patch.object(transport.host,'_preflight',side_effect=[None,transport.host._Changed()]), \
             patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), \
             patch.object(transport.host,'_exchange',return_value=b'{"messageId":"receipt"}') as run:
            with self.assertRaises(transport.GatewayPinnedStateChanged): transport.call(self.cfg,'/helper','a'*64,'chat.inject',{})
            self.assertEqual(run.call_count,1)

    def test_initial_pinned_input_change_has_fixed_code_and_never_starts_cli(self):
        with patch.object(transport.host,'_preflight',side_effect=transport.host._Changed()), \
             patch.object(transport.host,'_hash_file',return_value=('a'*64,1)), \
             patch.object(Path,'resolve',return_value=Path('/helper')), \
             patch.object(transport.host,'_exchange') as run:
            with self.assertRaises(transport.GatewayPinnedStateChanged):
                transport.call(self.cfg,'/helper','a'*64,'sessions.list',{})
            run.assert_not_called()

    @unittest.skipUnless(os.environ.get('MAIL_ROUTER_PROCESS_TESTS') == '1', 'opt-in subprocess test; strict gate forbids subprocess')
    def test_real_helper_uses_stdin_and_normal_cli_bootstrap(self):
        node=shutil.which('node')
        if not node: self.skipTest('node unavailable')
        helper=Path(__file__).resolve().parents[2]/'adapters/gateway-private-stdio.mjs'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dist=root/'dist';dist.mkdir()
            (root/'package.json').write_text('{"type":"module"}')
            (dist/'index.js').write_text("import {readFileSync} from 'node:fs';const args=process.argv;let raw='';try{raw=readFileSync('/proc/self/cmdline','utf8')}catch{}const message=JSON.parse(args[6]).message;const allowed=message==='synthetic-secret'||message==='x'||(typeof message==='string'&&message.length===12000&&message.charCodeAt(0)===0);if(args[2]!=='gateway'||args[3]!=='call'||args[4]!=='chat.inject'||!allowed||raw.includes('synthetic-secret'))process.exit(1);console.log(JSON.stringify({messageId:'receipt'}));")
            proc=subprocess.run([node,str(helper),str(root)],input=json.dumps({'method':'chat.inject','params':{'message':'synthetic-secret'}}).encode(),capture_output=True,timeout=10,check=True)
            self.assertEqual(json.loads(proc.stdout),{'messageId':'receipt'});self.assertEqual(proc.stderr,b'')
            bad=subprocess.run([node,str(helper),str(root)],input=b'{"method":"agent","params":{}}',capture_output=True,timeout=10)
            self.assertNotEqual(bad.returncode,0)
            escaped='\0'*12000
            large=subprocess.run([node,str(helper),str(root)],input=json.dumps({'method':'chat.inject','params':{'message':escaped}}).encode(),capture_output=True,timeout=10,check=True)
            self.assertEqual(json.loads(large.stdout),{'messageId':'receipt'});self.assertEqual(large.stderr,b'')
            valid=json.dumps({'method':'chat.inject','params':{'message':'x'}}).encode()
            exact=valid[:-1]+b' '*(96*1024-len(valid))+b'}'
            boundary=subprocess.run([node,str(helper),str(root)],input=exact,capture_output=True,timeout=10,check=True)
            self.assertEqual(json.loads(boundary.stdout),{'messageId':'receipt'});self.assertEqual(boundary.stderr,b'')
            oversized=valid[:-1]+b' '*(96*1024+1-len(valid))+b'}'
            rejected=subprocess.run([node,str(helper),str(root)],input=oversized,capture_output=True,timeout=10)
            self.assertNotEqual(rejected.returncode,0)

if __name__=='__main__':unittest.main()
