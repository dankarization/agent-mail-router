"""Host boundary checks; opt-in Linux tests spawn only local Python fixtures."""
from dataclasses import replace
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_mail_routing as routing
import agent_mail_routing_host as host


def sha(data):
    return hashlib.sha256(data).hexdigest()


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.parent = base / 'builds'; self.root = self.parent / 'fixture'
        for name in host._ROOTS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.commit = 'a' * 40
        for name in host._REQUIRED | {'node_modules/tsx/package.json'}:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            value = {'name': 'openclaw'} if name == 'package.json' else ({'commit': self.commit} if name == 'dist/build-info.json' else ({'name': 'tsx'} if name.endswith('tsx/package.json') else {}))
            path.write_text(json.dumps(value))
        self.node = base / 'node'; self.node.write_bytes(b'fake node')
        self.adapter = base / 'adapter'; self.adapter.write_bytes(b'fake adapter')
        self.config = base / 'config.json'; self.config.write_text('{}')
        self.policy = base / 'policy.json'; self.policy.write_text('{}')
        self.manifest = self.root / 'operator-artifact-manifest.json'
        self.write_manifest()
        self.cfg = host.TrustedHostConfig(str(self.node), sha(self.node.read_bytes()), str(self.adapter), sha(self.adapter.read_bytes()), str(self.parent), str(self.root), self.commit, str(self.manifest), sha(self.manifest.read_bytes()), str(self.config), sha(self.config.read_bytes()), str(self.policy), sha(self.policy.read_bytes()), 'main', ('main',), 30000)
        scope = routing.RoutingScope('owner', 'mail', ('main',), (), 'policy')
        fact = routing.SessionFact('agent:main:private:alpha', 'inc', 'owner', 'mail', 'main', 'private', 'Orchid telescope', 'Telescope project', ('lens',), True, True, True, False, False, False, False, False, False, False)
        self.source = routing.SelectionInput('message', 'general_fallback', False, 'unbound', 'new', 'Orchid telescope lens', True)
        self.options = routing.shortlist(routing.catalog(scope, (fact,), revision='rev', pages_complete=True), self.source)
        self.calls = []

    def write_manifest(self, mutate=None):
        files = []
        for path in self.root.rglob('*'):
            if path.is_file() and path != self.manifest:
                data = path.read_bytes(); files.append({'path': path.relative_to(self.root).as_posix(), 'bytes': len(data), 'mode': path.stat().st_mode & 0o7777, 'sha256': sha(data)})
        manifest = {'commit': self.commit, 'files': files, 'symlinks': []}
        if mutate: mutate(manifest)
        self.manifest.write_text(json.dumps(manifest))

    def run_host(self, raw=None, cfg=None, source=None, options=None, during=None):
        def exchange(argv, cwd, env, payload, end):
            self.calls.append((argv, cwd, env, json.loads(payload)))
            if during: during()
            return raw if raw is not None else b'{"version":1,"status":"ok","decisionRaw":"{}"}'
        return host.classify(cfg or self.cfg, source or self.source, options or self.options, exchange=exchange)

    def test_exact_monotonic_budget_schedule_and_insufficient_work(self):
        self.assertEqual(host._schedule(120.0, 100.0), (105.0, 106.0, 118.0))
        with self.assertRaises(host._Expired): host._schedule(120.0, 105.0)

    def test_exact_cooperative_and_force_boundaries(self):
        self.assertEqual(host._control_action(105.999, 106, 118, False, False), 'wait')
        self.assertEqual(host._control_action(106, 106, 118, False, False), 'term')
        self.assertEqual(host._control_action(117.999, 106, 118, True, False), 'wait')
        self.assertEqual(host._control_action(118, 106, 118, True, False), 'force')
        self.assertEqual(host._control_action(101, 106, 118, False, True), 'term')
        self.assertEqual(host._control_action(101, 106, 118, True, True), 'wait')

    def test_no_work_window_defers_before_exchange(self):
        cfg = replace(self.cfg, wall_timeout_ms=15000)
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_timeout')
        self.assertFalse(self.calls)

    def test_pinned_success_uses_closed_payload_and_trusted_host(self):
        result = self.run_host()
        self.assertEqual((result.status, result.decision_raw), ('ok', '{}'))
        argv, cwd, env, envelope = self.calls[0]
        self.assertEqual(cwd, self.root)
        self.assertEqual(argv, [str(self.node), '--import', str(self.root / 'scripts/tsx.mjs'), str(self.adapter)])
        self.assertEqual(env['OPENCLAW_CONFIG_PATH'], str(self.config))
        self.assertEqual(set(envelope), {'version', 'host', 'payload'})
        self.assertNotIn('private:alpha', json.dumps(envelope['payload']))
        self.assertNotIn('Orchid', json.dumps(argv))
        self.assertEqual(envelope['host']['agentId'], 'main')
        self.assertGreater(envelope['host']['wallTimeoutMs'], 15000)
        work_ms = int(env['OPENCLAW_MAIL_WORK_DEADLINE_MONOTONIC_MS'])
        self.assertGreater(work_ms, int(time.monotonic() * 1000))
        self.assertLessEqual(work_ms - int(time.monotonic() * 1000), 15000)

    def test_serialized_envelope_accepts_escaped_wire_over_64k_and_rejects_over_192k(self):
        payload = {'body': '\0' * 10500, 'candidates': [
            {'candidateId': 'c_' + format(i, '032x'), 'agentId': 'main',
             'title': 'x' * 100, 'purpose': 'test', 'aliases': []}
            for i in range(77)]}
        host_value = {'buildRoot': '/qa/release', 'expectedCommit': self.commit,
                      'manifestPath': '/qa/manifest', 'manifestSha256': 'a' * 64,
                      'configPath': '/qa/config', 'configSha256': 'b' * 64,
                      'policyPath': '/qa/policy', 'policySha256': 'c' * 64,
                      'agentId': 'main', 'wallTimeoutMs': 30000}
        wire = host._serialize_envelope(host_value, payload)
        self.assertGreater(len(wire), 65536)
        self.assertLessEqual(len(wire), host._MAX_ENVELOPE_BYTES)
        self.assertEqual(json.loads(wire)['payload']['body'], '\0' * 10500)
        low, high = 0, host._MAX_ENVELOPE_BYTES
        exact = None; exact_padding = None
        while low <= high:
            middle = (low + high) // 2
            candidate = host._serialize_envelope(dict(host_value, operator_fixture='x' * middle), payload)
            if candidate is not None and len(candidate) == host._MAX_ENVELOPE_BYTES:
                exact = candidate; exact_padding = middle; break
            if candidate is None: high = middle - 1
            else: low = middle + 1
        self.assertIsNotNone(exact)
        too_large = dict(host_value, operator_fixture='x' * (exact_padding + 1))
        self.assertIsNone(host._serialize_envelope(too_large, payload))

    def test_oversize_serialized_envelope_defers_before_exchange(self):
        with patch.object(host, '_serialize_envelope', return_value=None):
            result = self.run_host()
        self.assertEqual((result.status, result.error_code), ('deferred', 'classifier_input_invalid'))
        self.assertFalse(self.calls)

    def test_old_manifest_and_modified_critical_file_rejected_before_spawn(self):
        self.write_manifest(lambda m: next(x for x in m['files'] if x['path'] == 'package.json').pop('sha256'))
        cfg = replace(self.cfg, manifest_sha256=sha(self.manifest.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_runtime_unsupported')
        self.write_manifest(); cfg = replace(self.cfg, manifest_sha256=sha(self.manifest.read_bytes()))
        (self.root / 'src/config/config.ts').write_text('changed')
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_source_changed')
        self.assertFalse(self.calls)

    def test_config_pin_mismatch_is_rejected_before_classifier_exchange(self):
        self.config.write_text('{"approved":false}')
        self.assertEqual(self.run_host().error_code, 'classifier_source_changed')
        self.assertFalse(self.calls)

    def test_agreed_minimal_manifest_does_not_require_inventory_or_tsconfig(self):
        self.write_manifest(lambda m: m.pop('symlinks'))
        cfg = replace(self.cfg, manifest_sha256=sha(self.manifest.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).status, 'ok')

    def test_unlisted_noncritical_file_is_operator_immutable_root_responsibility(self):
        (self.root / 'src/unlisted.ts').write_text('not scanned per call')
        self.assertEqual(self.run_host().status, 'ok')

    def test_ordinary_escaped_config_values_are_accepted(self):
        self.config.write_text('{"label":"line\\nnext", "nested":{"value":"\\u0041"}}')
        cfg = replace(self.cfg, config_sha256=sha(self.config.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).status, 'ok')

    def test_nested_literal_include_rejected(self):
        self.config.write_text('{"nested":[{"$include":"elsewhere"}]}')
        cfg = replace(self.cfg, config_sha256=sha(self.config.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_runtime_unsupported')
        self.assertFalse(self.calls)

    def test_json5_config_rejected(self):
        self.config.write_text('{unquoted: true}')
        cfg = replace(self.cfg, config_sha256=sha(self.config.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_runtime_unsupported')
        self.assertFalse(self.calls)

    def test_escaped_include_and_unapproved_agent_rejected_before_spawn(self):
        self.config.write_text('{"nested":[{"\\u0024include":"elsewhere"}]}')
        cfg = replace(self.cfg, config_sha256=sha(self.config.read_bytes()))
        self.assertEqual(self.run_host(cfg=cfg).error_code, 'classifier_runtime_unsupported')
        self.assertEqual(self.run_host(cfg=replace(cfg, agent_id='foreign')).error_code, 'classifier_runtime_unsupported')
        self.assertFalse(self.calls)

    def test_changed_pinned_file_after_exchange_fails_closed(self):
        self.assertEqual(self.run_host(during=lambda: self.policy.write_text('{"changed":true}')).error_code, 'classifier_source_changed')

    def test_invalid_response_and_deferred_codes(self):
        for raw in (b'not-json', b'{"version":1,"version":1}', b'{"version":true,"status":"ok","decisionRaw":"{}"}', b'{"version":1,"status":"deferred","errorCode":{}}', b'x' * 8193):
            with self.subTest(raw=raw[:80]):
                self.assertEqual(self.run_host(raw=raw).error_code, 'classifier_output_invalid')
        self.assertEqual(self.run_host(raw=b'{"version":1,"status":"deferred","errorCode":"classifier_timeout"}').error_code, 'classifier_timeout')

    def test_exact_and_unknown_never_start_bridge(self):
        for field, value in [('exact_status', 'matched'), ('delivery_state', 'unknown')]:
            source = replace(self.source, **{field: value})
            self.assertNotEqual(self.run_host(source=source).status, 'ok')
        self.assertFalse(self.calls)

    def test_unsupported_platform_does_not_spawn(self):
        with patch.object(host.sys, 'platform', 'darwin'), patch.object(host.subprocess, 'Popen') as spawn:
            with self.assertRaises(host._Unsupported):
                host._exchange(['unused'], Path('/tmp'), {}, b'x', time.monotonic()+1)
            spawn.assert_not_called()

    def test_unsupported_native_abi_rejected_before_spawn(self):
        with patch.object(host.os, 'uname', return_value=type('U', (), {'machine': 'aarch64'})()), \
             patch.object(host.subprocess, 'Popen') as spawn:
            with self.assertRaises(host._Unsupported):
                host._exchange(['unused'], Path('/tmp'), {}, b'x', time.monotonic()+1)
            spawn.assert_not_called()

    def test_missing_proc_rejected_before_spawn(self):
        with patch.object(host.sys, 'platform', 'linux'), patch.object(host, '_proc_identity', side_effect=FileNotFoundError), patch.object(host.subprocess, 'Popen') as spawn:
            with self.assertRaises(host._Unsupported):
                host._exchange(['unused'], Path('/tmp'), {}, b'x', time.monotonic()+1)
            spawn.assert_not_called()

    def test_cleanup_never_signals_unverified_group(self):
        class Process:
            pid = 12345
            returncode = None
        identity = (12345, 12345, 12345, 55)
        with patch.object(host, '_proc_identity', return_value=('S', 54321, 12345, 55)), patch.object(host.os, 'killpg') as kill:
            with self.assertRaises(host._CleanupFailed): host._stop_group(Process(), identity, time.monotonic()+1)
            kill.assert_not_called()
        with patch.object(host, '_proc_identity', side_effect=FileNotFoundError), patch.object(host.os, 'killpg') as kill:
            with self.assertRaises(host._CleanupFailed): host._stop_group(Process(), identity, time.monotonic()+1)
            kill.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('MAIL_ROUTER_PROCESS_TESTS') == '1',
                     'opt-in local process tests; offline source gate forbids subprocess')
class ProcessCleanupTests(unittest.TestCase):
    def run_fixture(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            program = r"""
import os, signal, sys, time
mode, path = sys.argv[1:]
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == 'closed': os.close(1)
    with open(path, 'w') as f: f.write(str(os.getpid()))
    while True: time.sleep(1)
while not os.path.exists(path): time.sleep(.005)
if mode == 'term':
    while True: time.sleep(1)
print('{"ok":true}', flush=True)
"""
            try:
                start = time.monotonic()
                bounds = (start + 0.25, start + 0.35, start + 0.85)
                with self.assertRaises(host._CleanupFailed):
                    host._exchange([sys.executable, '-c', program, mode, str(pidfile)], Path(tmp), {}, b'x', start + 2, _bounds=bounds)
                child = int(pidfile.read_text())
                try:
                    state = host._proc_identity(child)[0]
                except FileNotFoundError:
                    state = 'gone'
                self.assertIn(state, ('Z', 'X', 'gone'))
            finally:
                if pidfile.exists():
                    try:
                        child = int(pidfile.read_text())
                        state = host._proc_identity(child)[0]
                        if state not in ('Z', 'X'): os.kill(child, signal.SIGKILL)
                    except (FileNotFoundError, ProcessLookupError):
                        pass

    def test_native_nocldwait_rejected_before_spawn_without_mutation(self):
        program = r"""
import ctypes, signal, time
from pathlib import Path
from unittest.mock import patch
import agent_mail_routing_host as host

class Action(ctypes.Structure):
    _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_ulong * 16),
                ('flags', ctypes.c_int), ('restorer', ctypes.c_void_p)]
libc = ctypes.CDLL(None, use_errno=True)
sigaction = libc.sigaction
sigaction.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(Action)]
sigaction.restype = ctypes.c_int
before = Action()
assert sigaction(signal.SIGCHLD, None, ctypes.byref(before)) == 0
set_action = Action()
ctypes.memmove(ctypes.byref(set_action), ctypes.byref(before), ctypes.sizeof(Action))
set_action.handler = None
set_action.flags |= 2
assert sigaction(signal.SIGCHLD, ctypes.byref(set_action), None) == 0
assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
with patch.object(host.subprocess, 'Popen', side_effect=AssertionError('spawn called')) as spawn, \
     patch.object(host.os, 'pipe', side_effect=AssertionError('pipe created')) as pipe:
    try:
        host._exchange(['unused'], Path('/tmp'), {}, b'x', time.monotonic()+2)
    except host._Unsupported:
        pass
    else:
        raise AssertionError('SA_NOCLDWAIT accepted')
    spawn.assert_not_called()
    pipe.assert_not_called()
after = Action()
assert sigaction(signal.SIGCHLD, None, ctypes.byref(after)) == 0
assert after.handler is None and after.flags & 2
print('rejected')
"""
        proc = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=5, check=True)
        self.assertEqual(proc.stdout, b'rejected\n')
        self.assertEqual(proc.stderr, b'')

    def test_real_competing_sigchld_handler_rejected_before_spawn(self):
        program = r"""
import os, signal, time
from pathlib import Path
from unittest.mock import patch
import agent_mail_routing_host as host

def competing_handler(signum, frame):
    try: os.waitpid(-1, os.WNOHANG)
    except ChildProcessError: pass

signal.signal(signal.SIGCHLD, competing_handler)
with patch.object(host.subprocess, 'Popen', side_effect=AssertionError('spawn called')) as spawn, \
     patch.object(host.os, 'pipe', side_effect=AssertionError('pipe created')) as pipe:
    try:
        host._exchange(['unused'], Path('/tmp'), {}, b'x', time.monotonic()+2)
    except host._Unsupported:
        pass
    else:
        raise AssertionError('custom handler accepted')
    assert signal.getsignal(signal.SIGCHLD) is competing_handler
    spawn.assert_not_called()
    pipe.assert_not_called()
print('rejected')
"""
        proc = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=5, check=True)
        self.assertEqual(proc.stdout, b'rejected\n')
        self.assertEqual(proc.stderr, b'')

    def test_clean_success_returns_output_after_reaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            output = host._exchange([sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read())'],
                                    Path(tmp), {}, b'{"version":1,"status":"deferred","errorCode":"classifier_timeout"}', start+3, _bounds=(start+1, start+1.1, start+2))
            self.assertEqual(output, b'{"version":1,"status":"deferred","errorCode":"classifier_timeout"}')

    def test_cooperative_term_allows_leader_cleanup_before_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'drained'
            program = r"""
import signal,sys,time
marker=sys.argv[1]
def close(signum, frame):
    time.sleep(.15)
    open(marker, 'w').write('drained')
    print('{"version":1,"status":"deferred","errorCode":"classifier_timeout"}', flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, close)
while True: time.sleep(.05)
"""
            start = time.monotonic()
            output = host._exchange([sys.executable, '-c', program, str(marker)], Path(tmp), {}, b'x',
                                    start+2, _bounds=(start+.2, start+.3, start+1.3))
            self.assertIn(b'classifier_timeout', output)
            self.assertEqual(marker.read_text(), 'drained')
            self.assertGreaterEqual(time.monotonic()-start, .4)

    def test_malformed_output_gets_cooperative_cleanup_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'drained'
            program = r"""
import os,signal,sys,time
marker=sys.argv[1]
def close(signum, frame):
    time.sleep(.15)
    open(marker, 'w').write('drained')
    sys.exit(0)
signal.signal(signal.SIGTERM, close)
print('not-json', flush=True)
os.close(1)
while True: time.sleep(.05)
"""
            start = time.monotonic()
            with self.assertRaises(host._OutputInvalid):
                host._exchange([sys.executable, '-c', program, str(marker)], Path(tmp), {}, b'x',
                               start+2, _bounds=(start+.8, start+.9, start+1.5))
            self.assertEqual(marker.read_text(), 'drained')
            self.assertGreaterEqual(time.monotonic()-start, .15)

    def test_exited_leader_child_holds_stdout(self):
        self.run_fixture('open')

    def test_exited_leader_child_closes_stdout(self):
        self.run_fixture('closed')

    def test_timeout_term_leader_exits_child_ignores_term(self):
        self.run_fixture('term')


if __name__ == '__main__':
    unittest.main()
