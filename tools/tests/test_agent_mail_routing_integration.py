"""Admission observation uses real SQLite/store without delivering or replaying mail."""
import tempfile
import io
import json
from contextlib import redirect_stdout
import unittest
from pathlib import Path
from unittest.mock import patch
import agent_mail_router as router
import agent_mail_routing_integration as integration
import agent_mail_routing as routing
from agent_mail_routing_host import BridgeResult
from unittest.mock import Mock
from test_agent_mail_router import msg, snap, ROOT


class TestInboundObservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        routes = router.routes_at(ROOT / 'examples/routes.example.json')
        routes['general'] = {'id': 'general', 'title': 'General',
            'parent': 'agent:main:telegram:direct:900000001:thread:900000001:9001',
            'children': [], 'fallback': True, 'message_ids': [], 'task_refs': [],
            'evidence': 'Synthetic operator assignment'}
        self.store = router.Store(Path(self.tmp.name) / 'mail.sqlite3', routes)
        self.addCleanup(self.store.db.close)
        self.store.bootstrap({'cursor': {'uidvalidity': '77', 'last_uid': 0}})

    def test_actual_new_poll_fails_closed_without_mutation(self):
        first, second = msg(1, 'no exact binding'), msg(2, 'another request')
        self.store.ingest(snap([first, second]))
        before = self.store.db.total_changes
        with patch.object(router.Gateway, 'call', side_effect=AssertionError('RPC forbidden')):
            out = integration.observe_new_ingest(self.store, [first, second])
        self.assertEqual(out['reason'], 'semantic_delivery_unavailable')
        self.assertEqual(out['reasons'], {'atomic_incarnation_fence_unavailable': 2})
        self.assertEqual(before, self.store.db.total_changes)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'semantic_%'").fetchone()[0], 0)

    def test_new_exact_binding_and_conflicts_stay_with_existing_routing(self):
        routes = [r for r in self.store.routes.values() if not r.get('fallback')]
        with self.store.db:
            self.store.db.executemany('INSERT INTO anchors VALUES (?,?)',
                [('<one@test.local>', routes[0]['id']), ('<two@test.local>', routes[1]['id'])])
        a, b = msg(1, reply='<one@test.local>'), msg(2, refs='<one@test.local> <two@test.local>')
        self.store.ingest(snap([a, b]))
        out = integration.observe_new_ingest(self.store, [a, b])
        self.assertEqual(out['reasons'], {'exact_or_triage': 2})
        rows = self.store.db.execute('SELECT route,reason FROM mail ORDER BY key').fetchall()
        self.assertEqual(rows[0]['reason'], 'thread_identity')
        self.assertEqual(rows[1]['reason'], 'conflicting_thread_ids')
        self.assertIsNone(rows[1]['route'])

    def test_no_historical_replay_or_ambiguous_duplicate_poll(self):
        first = msg(1, 'old')
        self.store.ingest(snap([first]))
        self.store.batch()
        self.assertEqual(integration.observe_new_ingest(self.store, [])['examined'], 0)
        self.assertEqual(integration.observe_new_ingest(self.store, [first, first])['reason'], 'snapshot_duplicate_keys')
        self.assertEqual(integration.observe_new_ingest(self.store, [first])['reasons'], {'already_batched': 1})

    def test_cli_tick_observes_new_mail_before_batch_without_external_calls(self):
        routes = json.loads((ROOT / 'examples/routes.example.json').read_text())
        routes['routes'].append({'id': 'general', 'title': 'General',
            'parent': 'agent:main:telegram:direct:900000001:thread:900000001:9001',
            'children': [], 'fallback': True, 'message_ids': [], 'task_refs': [],
            'evidence': 'Synthetic operator assignment'})
        path = Path(self.tmp.name) / 'routes.json'
        path.write_text(json.dumps(routes))
        message = msg(1, 'unmatched synthetic request')
        out = io.StringIO()
        with patch.object(router, 'poll', return_value=snap([message])), \
             patch.object(router.Gateway, 'call', side_effect=AssertionError('RPC forbidden')), \
             patch.object(router.Telegram, 'send', side_effect=AssertionError('Telegram forbidden')), \
             redirect_stdout(out):
            code = router.main(['--state-dir', self.tmp.name, '--routes', str(path),
                                'tick', '--semantic-preflight'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['semantic_preflight']['reasons'],
                         {'atomic_incarnation_fence_unavailable': 1})
        self.assertTrue(result['fire'])
        self.assertEqual(result['batch']['topics'][0]['route_id'], 'general')

    def synthetic_catalog(self):
        scope = routing.RoutingScope('owner', 'mail', ('main',), (), 'policy-1')
        fact = routing.SessionFact('agent:main:private:one', 'inc-1', 'owner', 'mail', 'main',
            'private', 'Orchid telescope', 'Telescope project', ('lens',), True, True, True,
            False, False, False, False, False, False, False)
        return routing.catalog(scope, (fact,), revision='r1', pages_complete=True)

    def test_selector_once_advisory_only_with_persistent_decision(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message]))
        cat = self.synthetic_catalog()
        def select(source, options):
            raw = json.dumps({'candidateId': options.entries[0].candidate_id, 'confidence': 'high',
                              'alternatives': [], 'evidence': ['Orchid telescope'], 'reasonCode': 'matched'})
            return BridgeResult('ok', decision_raw=raw)
        classifier = Mock(side_effect=select)
        out = integration.select_advisory(self.store, message['key'], cat, classifier)
        self.assertEqual((out['status'], out['reason'], out['decision'].status),
                         ('held', 'delivery_not_enabled', 'selected'))
        again = integration.select_advisory(self.store, message['key'], cat, classifier)
        self.assertEqual(again['decision'], out['decision'])
        classifier.assert_called_once()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT route FROM mail WHERE key=?', (message['key'],)).fetchone()[0], 'general')
        values = ''.join(r[0] for r in self.store.db.execute("SELECT value FROM meta WHERE key LIKE 'semantic_%'"))
        self.assertNotIn('Orchid telescope', values)

    def test_selector_unknown_or_changed_input_never_replays(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message])); cat = self.synthetic_catalog()
        def modified(source, options):
            changed = dict(message, body='Orchid telescope lens changed')
            with self.store.db:
                self.store.db.execute('UPDATE mail SET payload=? WHERE key=?', (json.dumps(changed), message['key']))
            return BridgeResult('ok', decision_raw='{}')
        classifier = Mock(side_effect=modified)
        self.assertEqual(integration.select_advisory(self.store, message['key'], cat, classifier)['reason'],
                         'classifier_or_source_changed')
        self.assertEqual(integration.select_advisory(self.store, message['key'], cat, classifier)['reason'],
                         'classifier_reservation_held')
        classifier.assert_called_once()

    def test_selector_keeps_external_mail_report_only(self):
        message = dict(msg(1, 'Orchid telescope lens'), sender='unknown@example.invalid', trust_class='unknown')
        message['content_request'] = router.source_for(message)
        self.store.ingest(snap([message]))
        # Source resolution is normally done through the content stage. Only
        # emulate the resulting complete, durable message in synthetic SQLite.
        message.pop('content_request')
        with self.store.db:
            self.store.db.execute('UPDATE mail SET payload=? WHERE key=?', (json.dumps(message), message['key']))
        def select(source, options):
            self.assertTrue(source.source_report_only)
            return BridgeResult('ok', decision_raw=json.dumps({'candidateId': options.entries[0].candidate_id,
                'confidence': 'high', 'alternatives': [], 'evidence': ['Orchid telescope'], 'reasonCode': 'matched'}))
        out = integration.select_advisory(self.store, message['key'], self.synthetic_catalog(), select)
        self.assertTrue(out['decision'].source_report_only)
        self.assertEqual(out['status'], 'held')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)

    def test_classifier_failure_keeps_reservation_unknown(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message])); classifier = Mock(return_value=BridgeResult('deferred', error_code='classifier_timeout'))
        cat = self.synthetic_catalog()
        self.assertEqual(integration.select_advisory(self.store, message['key'], cat, classifier)['reason'],
                         'classifier_or_source_changed')
        self.assertEqual(integration.select_advisory(self.store, message['key'], cat, classifier)['reason'],
                         'classifier_reservation_held')
        classifier.assert_called_once()

    def test_selector_rejects_incomplete_and_historical_without_call(self):
        message = dict(msg(1, 'Orchid telescope lens'), body_truncated=True)
        self.store.ingest(snap([message])); classifier = Mock()
        self.assertEqual(integration.select_advisory(self.store, message['key'], self.synthetic_catalog(), classifier)['reason'],
                         'mail_body_incomplete')
        self.store.batch()
        self.assertEqual(integration.select_advisory(self.store, message['key'], self.synthetic_catalog(), classifier)['reason'],
                         'mail_not_admissible')
        classifier.assert_not_called()

    def test_corrupt_or_oversize_snapshot_never_runs_preflight(self):
        with patch.object(integration, 'preflight', side_effect=AssertionError('must not run')):
            self.assertEqual(integration.observe_new_ingest(self.store, [{}])['reason'], 'snapshot_invalid')
            self.assertEqual(integration.observe_new_ingest(self.store, [{}] * 101)['reason'], 'snapshot_invalid_or_too_large')

if __name__ == '__main__':
    unittest.main()

class TestSemanticProcessor(TestInboundObservation):
    def test_processor_selects_then_delegates_fenced_passive_delivery(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message])); cat = self.synthetic_catalog()
        def classify(source, options):
            return BridgeResult('ok', decision_raw=json.dumps({'candidateId': options.entries[0].candidate_id,
                'confidence':'high', 'alternatives':[], 'evidence':['Orchid telescope'], 'reasonCode':'matched'}))
        classifier = Mock(side_effect=classify)
        def deliver(db, path, catalog, source, options, decision, rpc, source_is_current):
            self.assertIs(db, self.store.db); self.assertEqual(decision.status, 'selected')
            self.assertTrue(source_is_current())
            return {'status':'delivered', 'taskStarted':False}
        with patch('agent_mail_gateway_catalog.load_scope', return_value=({}, cat.scope)), \
             patch('agent_mail_gateway_catalog.collect', return_value=cat), \
             patch('agent_mail_gateway_delivery.deliver', side_effect=deliver) as delivery:
            result = integration.process_semantic_context(self.store, message['key'], '/synthetic', lambda *a: {}, classifier)
            again = integration.process_semantic_context(self.store, message['key'], '/synthetic', lambda *a: {}, classifier)
        self.assertEqual(result, {'status':'delivered', 'taskStarted':False})
        self.assertEqual(again['status'], 'delivered')
        classifier.assert_called_once()
        self.assertEqual(delivery.call_count, 2)

    def test_processor_changed_source_cannot_deliver_or_retry_classifier(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message])); cat = self.synthetic_catalog()
        def classify(source, options):
            changed = dict(message, body='Orchid telescope lens changed')
            with self.store.db:
                self.store.db.execute('UPDATE mail SET payload=? WHERE key=?', (json.dumps(changed), message['key']))
            return BridgeResult('ok', decision_raw='{}')
        classifier = Mock(side_effect=classify)
        with patch('agent_mail_gateway_catalog.load_scope', return_value=({}, cat.scope)), \
             patch('agent_mail_gateway_catalog.collect', return_value=cat), \
             patch('agent_mail_gateway_delivery.deliver') as delivery:
            result = integration.process_semantic_context(self.store, message['key'], '/synthetic', lambda *a: {}, classifier)
            again = integration.process_semantic_context(self.store, message['key'], '/synthetic', lambda *a: {}, classifier)
        self.assertEqual(result['reason'], 'classifier_or_source_changed')
        self.assertEqual(again['reason'], 'classifier_reservation_held')
        classifier.assert_called_once(); delivery.assert_not_called()

    def test_processor_never_classifies_batched_mail(self):
        message = msg(1, 'Orchid telescope lens')
        self.store.ingest(snap([message])); self.store.batch(); classifier = Mock()
        result = integration.process_semantic_context(self.store, message['key'], '/synthetic', lambda *a: {}, classifier)
        self.assertEqual(result['reason'], 'mail_not_admissible'); classifier.assert_not_called()

    def test_complete_synthetic_chain_uses_actual_catalog_and_delivery_journal(self):
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture = CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        message = msg(1, 'WALT funding collection')
        self.store.ingest(snap([message])); calls = []
        def rpc(method, params):
            calls.append((method, params))
            if method == 'sessions.list':
                return {'sessions':[fixture.row], 'count':1, 'totalCount':1, 'hasMore':False}
            if method == 'chat.inject':
                return {'messageId':'synthetic-receipt'}
            raise AssertionError(method)
        def classify(source, options):
            return BridgeResult('ok', decision_raw=json.dumps({'candidateId':options.entries[0].candidate_id,
                'confidence':'high', 'alternatives':[], 'evidence':['WALT funding'], 'reasonCode':'matched'}))
        classifier = Mock(side_effect=classify)
        result = integration.process_semantic_context(self.store, message['key'], fixture.path, rpc, classifier)
        self.assertEqual(result['status'], 'delivered')
        self.assertFalse(result['taskStarted'])
        self.assertEqual(integration.process_semantic_context(self.store, message['key'], fixture.path, rpc, classifier)['status'], 'already_delivered')
        classifier.assert_called_once()
        injections = [params for method, params in calls if method == 'chat.inject']
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0]['sessionId'], 'incarnation-1')

    def test_queued_before_batch_processes_after_batch_once(self):
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture = CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        message = msg(1, 'WALT funding collection'); self.store.ingest(snap([message]))
        self.assertEqual(integration.enqueue_new_ingest(self.store, [message])['queued'], 1)
        self.assertEqual(integration.enqueue_new_ingest(self.store, [message])['queued'], 0)
        self.store.batch(); calls = []
        def rpc(method, params):
            calls.append((method, params))
            if method == 'sessions.list':
                return {'sessions':[fixture.row], 'count':1, 'totalCount':1, 'hasMore':False}
            if method == 'chat.inject': return {'messageId':'queue-receipt'}
            raise AssertionError(method)
        def classify(source, options):
            return BridgeResult('ok', decision_raw=json.dumps({'candidateId':options.entries[0].candidate_id,
                'confidence':'high', 'alternatives':[], 'evidence':['WALT funding'], 'reasonCode':'matched'}))
        classifier = Mock(side_effect=classify)
        result = integration.run_one_queued(self.store, fixture.path, rpc, classifier)
        self.assertEqual(result['status'], 'delivered'); self.assertFalse(result['taskStarted'])
        self.assertEqual(integration.run_one_queued(self.store, fixture.path, rpc, classifier)['status'], 'idle')
        classifier.assert_called_once()
        self.assertEqual(len([x for x in calls if x[0] == 'chat.inject']), 1)

    def test_queue_does_not_replay_old_or_changed_source(self):
        message = msg(1, 'WALT funding collection'); self.store.ingest(snap([message]))
        self.assertEqual(integration.enqueue_new_ingest(self.store, [message])['queued'], 1)
        changed = dict(message, body='changed after queue')
        with self.store.db:
            self.store.db.execute('UPDATE mail SET payload=? WHERE key=?', (json.dumps(changed), message['key']))
        classifier = Mock(); rpc = Mock()
        result = integration.run_one_queued(self.store, '/unused', rpc, classifier)
        self.assertEqual(result['reason'], 'mail_not_admissible')
        self.assertEqual(integration.run_one_queued(self.store, '/unused', rpc, classifier)['status'], 'idle')
        classifier.assert_not_called(); rpc.assert_not_called()
        self.store.batch()
        self.assertEqual(integration.enqueue_new_ingest(self.store, [message])['queued'], 0)

    def test_queue_unknown_claim_is_not_replayed(self):
        message = msg(1, 'WALT funding collection'); self.store.ingest(snap([message]))
        integration.enqueue_new_ingest(self.store, [message])
        with patch.object(integration, 'process_semantic_context', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                integration.run_one_queued(self.store, '/unused', Mock(), Mock())
        self.assertEqual(integration.run_one_queued(self.store, '/unused', Mock(), Mock())['status'], 'idle')

    def test_ingest_enqueues_atomically_with_cursor(self):
        message = msg(1, 'WALT funding collection')
        result = self.store.ingest(snap([message]), semantic_enqueue=True)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(self.store.get('cursor')['last_uid'], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'semantic_queue_v1:%'").fetchone()[0], 1)

    def test_ingest_rolls_back_cursor_and_mail_if_enqueue_fails(self):
        message = msg(1, 'WALT funding collection')
        with patch.object(integration, 'enqueue_new_ingest', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                self.store.ingest(snap([message]), semantic_enqueue=True)
        self.assertEqual(self.store.get('cursor')['last_uid'], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mail').fetchone()[0], 0)

    def test_cli_tick_enqueues_without_running_classifier(self):
        routes = json.loads((ROOT / 'examples/routes.example.json').read_text())
        routes['routes'].append({'id':'general','title':'General',
            'parent':'agent:main:telegram:direct:900000001:thread:900000001:9001',
            'children':[], 'fallback':True, 'message_ids':[], 'task_refs':[],
            'evidence':'Synthetic operator assignment'})
        path = Path(self.tmp.name) / 'routes.json'; path.write_text(json.dumps(routes))
        message = msg(1, 'WALT funding collection'); out = io.StringIO()
        with patch.object(router, 'poll', return_value=snap([message])), \
             patch.object(router.Gateway, 'call', side_effect=AssertionError('no RPC')), \
             patch('agent_mail_routing_host.classify', side_effect=AssertionError('no model')), redirect_stdout(out):
            code = router.main(['--state-dir', self.tmp.name, '--routes', str(path), 'tick', '--semantic-enqueue'])
        self.assertEqual(code, 0); self.assertEqual(json.loads(out.getvalue())['semantic_queue']['queued'], 1)

    def test_empty_poll_enqueues_nothing_and_advances_safely(self):
        result = self.store.ingest(snap([], last=0), semantic_enqueue=True)
        self.assertEqual(result['queued'], 0)
        self.assertEqual(result['examined'], 0)
        self.assertEqual(self.store.get('cursor')['last_uid'], 0)

    def make_mime_content(self, message, body):
        from email.message import EmailMessage
        from agent_mail_content import identity, materialize
        root = Path(self.tmp.name) / 'content'; root.mkdir(exist_ok=True)
        self.store.content_root = root
        job = root / identity(message); job.mkdir()
        mail = EmailMessage(); mail['From'] = message['sender']; mail['To'] = message['to']
        mail['Subject'] = message['subject']; mail['Message-ID'] = message['message_id']
        mail.set_content(body)
        original = job / 'original.eml'; original.write_bytes(mail.as_bytes())
        return materialize(original, job, message, root, report_only=True)

    def test_real_snapshot_content_request_queues_and_uses_full_verified_body(self):
        from agent_mail_content import source_for
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture = CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        message = msg(1, 'unrelated preview'); message['content_request'] = source_for(message)
        self.store.ingest(snap([message]))
        self.assertEqual(integration.enqueue_new_ingest(self.store, [message])['queued'], 1)
        self.store.batch(); self.make_mime_content(message, 'WALT funding collection')
        calls=[]
        def rpc(method, params):
            calls.append((method, params))
            return {'sessions':[fixture.row], 'count':1, 'totalCount':1, 'hasMore':False} if method == 'sessions.list' else {'messageId':'full-body-receipt'}
        def classify(source, options):
            self.assertIn('WALT funding collection',source.body); self.assertNotIn('unrelated preview',source.body)
            return BridgeResult('ok',decision_raw=json.dumps({'candidateId':options.entries[0].candidate_id,
                'confidence':'high','alternatives':[],'evidence':['WALT funding'],'reasonCode':'matched'}))
        prepare=Mock(return_value=True)
        result=integration.run_one_queued(self.store, fixture.path, rpc, classify, content_prepare=prepare)
        self.assertEqual(result['status'],'delivered'); prepare.assert_called_once_with(message['key'])
        self.assertEqual(len([x for x in calls if x[0]=='chat.inject']),1)

    def test_full_content_tamper_during_inference_prevents_delivery(self):
        from agent_mail_content import source_for
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture = CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        message=msg(1,'preview'); message['content_request']=source_for(message)
        self.store.ingest(snap([message])); integration.enqueue_new_ingest(self.store,[message])
        manifest=self.make_mime_content(message,'WALT funding collection')
        def rpc(method, params):
            if method == 'sessions.list': return {'sessions':[fixture.row], 'count':1, 'totalCount':1, 'hasMore':False}
            raise AssertionError('must not deliver')
        def classify(source, options):
            Path(manifest['parts'][0]['text']['path']).write_text('tampered')
            return BridgeResult('ok',decision_raw='{}')
        result=integration.run_one_queued(self.store,fixture.path,rpc,classify,content_prepare=lambda key:True)
        self.assertEqual(result['reason'],'classifier_or_source_changed')

    def test_missing_and_oversize_full_body_are_never_classified(self):
        from agent_mail_content import source_for
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture=CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        for uid, body in ((1, None), (2, 'WALT funding ' + 'x'*13000)):
            message=msg(uid,'preview'); message['content_request']=source_for(message)
            self.store.ingest(snap([message])); integration.enqueue_new_ingest(self.store,[message])
            if body is not None: self.make_mime_content(message,body)
            classifier=Mock(); rpc=Mock()
            result=integration.run_one_queued(self.store,fixture.path,rpc,classifier,content_prepare=lambda key:True)
            self.assertEqual(result['reason'],'mail_not_admissible')
            classifier.assert_not_called(); rpc.assert_not_called()

    def test_changed_original_is_not_accepted_as_full_body(self):
        from agent_mail_content import source_for
        message=msg(1,'preview'); message['content_request']=source_for(message)
        manifest=self.make_mime_content(message,'WALT funding collection')
        Path(manifest['original']['path']).write_text('tampered original')
        self.assertIsNone(integration._full_content_body(self.store,message))

    def test_external_full_content_routes_as_report_only_without_work(self):
        from agent_mail_content import source_for
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture=CatalogTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        message=msg(1,'preview'); message['sender']='external@example.invalid'; message['trust_class']='unknown'
        message['content_request']=source_for(message)
        self.store.ingest(snap([message])); self.assertEqual(integration.enqueue_new_ingest(self.store,[message])['queued'],1)
        self.make_mime_content(message,'WALT funding collection')
        calls=[]
        def rpc(method,params):
            calls.append((method,params))
            return {'sessions':[fixture.row], 'count':1, 'totalCount':1, 'hasMore':False} if method=='sessions.list' else {'messageId':'receipt'}
        def classify(source,options):
            self.assertTrue(source.source_report_only)
            return BridgeResult('ok',decision_raw=json.dumps({'candidateId':options.entries[0].candidate_id,'confidence':'high',
                'alternatives':[],'evidence':['WALT funding'],'reasonCode':'matched'}))
        result=integration.run_one_queued(self.store,fixture.path,rpc,classify,content_prepare=lambda key:True)
        self.assertEqual(result['status'],'delivered'); self.assertTrue(result['sourceReportOnly'])
        self.assertFalse(result['taskStarted']);self.assertEqual({x[0] for x in calls},{'sessions.list','chat.inject'})

    def test_two_concurrent_consumers_admit_exactly_one_classifier(self):
        import sqlite3
        import threading
        from test_agent_mail_gateway_catalog import CatalogTests
        fixture=CatalogTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        message=msg(1,'WALT funding collection');self.store.ingest(snap([message]));integration.enqueue_new_ingest(self.store,[message])
        entered=threading.Event();release=threading.Event();results=[];calls=[]
        def rpc(method,params):
            calls.append(method)
            if method=='sessions.list': return {'sessions':[fixture.row],'count':1,'totalCount':1,'hasMore':False}
            return {'messageId':'receipt'}
        def classify(source, options):
            entered.set()
            if not release.wait(5): raise AssertionError('test timed out')
            return BridgeResult('ok',decision_raw=json.dumps({'candidateId':options.entries[0].candidate_id,
                'confidence':'high','alternatives':[],'evidence':['WALT funding'],'reasonCode':'matched'}))
        def consumer():
            other=router.Store.__new__(router.Store);other.state_dir=self.store.state_dir;other.routes=self.store.routes
            other.db=sqlite3.connect(self.store.state_dir/'mail.sqlite3',timeout=1);other.db.row_factory=sqlite3.Row
            try:results.append(integration.run_one_queued(other,fixture.path,rpc,classify))
            finally:other.db.close()
        thread=threading.Thread(target=consumer);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            second=integration.run_one_queued(self.store,fixture.path,rpc,Mock(side_effect=AssertionError('duplicate classifier')))
            self.assertEqual(second['status'],'idle')
        finally:
            release.set();thread.join(10)
        self.assertFalse(thread.is_alive());self.assertEqual(results[0]['status'],'delivered')
        self.assertEqual(calls.count('chat.inject'),1)
