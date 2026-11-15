"""Inbound preflight against real local Store; no model, RPC or delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import agent_mail_inbound as inbound
import agent_mail_router as router
from test_agent_mail_router import msg, snap, result, ROOT


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.routes = router.routes_at(ROOT / 'examples/routes.example.json')
        general = {'id': 'general', 'title': 'General',
                   'parent': 'agent:main:telegram:direct:900000001:thread:900000001:9001',
                   'children': [], 'fallback': True, 'message_ids': [], 'task_refs': [],
                   'evidence': 'Synthetic operator assignment'}
        self.routes['general'] = general
        self.store = router.Store(Path(self.tmp.name) / 'mail.sqlite3', self.routes)
        self.addCleanup(self.store.db.close)
        self.store.bootstrap({'cursor': {'uidvalidity': '77', 'last_uid': 0}})

    def ingest(self, message):
        self.store.ingest(snap([message]))
        return message['key']

    def test_new_fallback_held_before_classifier_or_durable_effect(self):
        key = self.ingest(msg(1, 'unrelated synthetic message'))
        before = self.store.db.total_changes
        self.assertEqual(self.store.semantic_preflight(key).reason, 'atomic_incarnation_fence_unavailable')
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM meta WHERE key LIKE "semantic_%"').fetchone()[0], 0)

    def test_external_report_only_fallback_is_held_without_work_or_effect(self):
        message = dict(msg(1), sender='unknown@example.invalid', trust_class='unknown')
        message['content_request'] = router.source_for(message)
        key = self.ingest(message)
        row = self.store.db.execute('SELECT reason FROM mail WHERE key=?', (key,)).fetchone()
        self.assertEqual(row['reason'], 'general_external_report')
        self.assertEqual(self.store.semantic_preflight(key).reason, 'atomic_incarnation_fence_unavailable')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)

    def test_exact_binding_and_conflict_are_not_semantic_candidates(self):
        first = next(r for r in self.routes.values() if not r.get('fallback'))
        second = next(r for r in self.routes.values() if not r.get('fallback') and r['id'] != first['id'])
        with self.store.db:
            self.store.db.execute('INSERT INTO anchors VALUES (?,?)', ('<first@test.local>', first['id']))
            self.store.db.execute('INSERT INTO anchors VALUES (?,?)', ('<second@test.local>', second['id']))
        one, two = msg(1, reply='<first@test.local>'), msg(2, refs='<first@test.local> <second@test.local>')
        self.store.ingest(snap([one, two]))
        self.assertEqual(self.store.semantic_preflight(one['key']).reason, 'exact_or_triage')
        self.assertEqual(self.store.semantic_preflight(two['key']).reason, 'exact_or_triage')

    def test_new_exact_anchor_wins_before_any_model_call(self):
        message = msg(1, refs='<new@test.local>'); key = self.ingest(message)
        target = next(r for r in self.routes.values() if not r.get('fallback'))
        with self.store.db:
            self.store.db.execute('INSERT INTO anchors VALUES (?,?)', ('<new@test.local>', target['id']))
        self.assertEqual(self.store.semantic_preflight(key).reason, 'exact_or_policy_changed')

    def test_existing_batch_or_unknown_delivery_cannot_replay(self):
        key = self.ingest(msg(1))
        batch = self.store.batch()
        self.assertEqual(self.store.semantic_preflight(key).reason, 'already_batched')
        self.store.complete(batch['batch_id'], [result(topic['route_id']) for topic in batch['topics']])
        with self.store.db:
            self.store.db.execute("UPDATE effects SET status='unknown' WHERE kind='context'")
        self.assertEqual(self.store.semantic_preflight(key).reason, 'already_summarized_or_effect_unknown')

    def test_fallback_rpc_reads_only_operator_key_and_returns_no_grant(self):
        parent = self.routes['general']['parent']
        row = {'key': parent, 'sessionId': 'synthetic-incarnation',
               'createdActor': {'type': 'human', 'id': 'synthetic-owner'},
               'owner': {'actor': {'type': 'human', 'id': 'synthetic-owner'}}}
        gateway = Mock()
        gateway.call.return_value = {'sessions': [row], 'hasMore': False}
        observed = self.store.semantic_fallback_observation(gateway)
        self.assertEqual(observed, inbound.FallbackObservation('synthetic-owner', 'synthetic-incarnation'))
        self.assertFalse(hasattr(observed, 'can_deliver'))
        gateway.call.assert_called_once_with('sessions.list', {'search': parent, 'limit': 10, 'rowMode': 'sidebar',
            'includeDerivedTitles': False, 'includeLastMessage': False, 'includeActivitySummary': False})
        self.assertEqual(self.ingest(msg(1)), '77:1')
        self.assertEqual(self.store.semantic_preflight('77:1').reason, 'atomic_incarnation_fence_unavailable')

    def test_fallback_rpc_incomplete_ambiguous_or_unmatched_is_not_observation(self):
        parent = self.routes['general']['parent']
        row = {'key': parent, 'sessionId': 'inc', 'createdActor': {'type': 'human', 'id': 'a'},
               'owner': {'actor': {'type': 'human', 'id': 'a'}}}
        gateway = Mock()
        invalid = [{'sessions': [row], 'hasMore': True}, {'sessions': [row]},
                   {'sessions': [row, row], 'hasMore': False},
                   {'sessions': [dict(row, owner={'actor': {'type': 'human', 'id': 'b'}})], 'hasMore': False},
                   {'sessions': [dict(row, archived=True)], 'hasMore': False},
                   {'sessions': [dict(row, sessionId='')], 'hasMore': False}]
        for reply in invalid:
            with self.subTest(reply=reply):
                gateway.call.return_value = reply
                self.assertIsNone(inbound.observe_fallback(gateway, self.routes))
        gateway.call.side_effect = RuntimeError('private diagnostic')
        self.assertIsNone(inbound.observe_fallback(gateway, self.routes))

    def test_unavailable_database_is_held(self):
        class BrokenDB:
            def execute(self, *args):
                raise RuntimeError('private diagnostic must not escape')
        class BrokenStore:
            db = BrokenDB()
        self.assertEqual(inbound.preflight(BrokenStore(), 'synthetic').reason, 'mail_state_invalid')

    def test_corrupt_batch_and_changed_fallback_are_held(self):
        key = self.ingest(msg(1))
        with self.store.db:
            self.store.db.execute('INSERT INTO batches VALUES (?,?,NULL)', ('corrupt', '{}'))
        self.assertEqual(self.store.semantic_preflight(key).reason, 'batch_state_invalid')
        with self.store.db: self.store.db.execute('DELETE FROM batches')
        self.routes['general']['fallback'] = False
        self.assertEqual(self.store.semantic_preflight(key).reason, 'fallback_policy_changed')


if __name__ == '__main__':
    unittest.main()
