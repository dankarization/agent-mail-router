"""Public-boundary recovery tests using invented input and fake transports."""
import base64
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import test_agent_mail_router as fixtures
from test_agent_mail_router import m, msg, result, snap, DAY


class RecoveryTests(fixtures.unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp
    tearDown = fixtures.RoutingTests.tearDown
    complete = fixtures.RoutingTests.complete

    def cli(self, *args, **patches):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(m, 'routes_at', return_value=self.routes), \
                patch.object(m, 'Gateway', return_value=self.gw), patch.object(m, 'Telegram', return_value=self.tg), \
                patch.object(m, 'poll', return_value=patches.get('snapshot', snap([], 0))), \
                patch.object(m.Ledger, 'dispatch_error', return_value={'status': 'quiet'}):
            code = m.main(['--state-dir', self.tmp.name, *args])
        return code, json.loads(output.getvalue())

    def test_field_length_and_type_diagnostics_are_bounded_and_payload_free(self):
        self.db.ingest(snap([msg(1, reply='<alpha@test.local>')]))
        bid = self.db.batch()['batch_id']
        for field, maximum in [('summary', 500), ('details', 1100), ('next', 300)]:
            for value in ('PRIVATE-' + 'x' * maximum, None, ''):
                with self.subTest(field=field, value_type=type(value).__name__):
                    code, out = self.cli('complete', '--batch', bid, '--result-json',
                                         json.dumps([dict(result('alpha'), **{field: value})]))
                    self.assertEqual(code, 1)
                    self.assertEqual(out['error'], 'summary_text_invalid')
                    self.assertEqual(out['validation']['field'], field)
                    self.assertEqual(out['validation']['maximum'], maximum)
                    self.assertEqual(out['validation']['actual_length'], len(value) if isinstance(value, str) else None)
                    self.assertLess(len(json.dumps(out)), 700)
                    self.assertNotIn('PRIVATE', json.dumps(out))

    def test_invalid_json_escape_position_without_raw_payload(self):
        raw = '[{"details":"C:\\Users\\PRIVATE"}]'
        for options in (['--result-json', raw], ['--result-b64', base64.b64encode(raw.encode()).decode()]):
            code, out = self.cli('complete', '--batch', 'any', *options)
            self.assertEqual(code, 1)
            self.assertEqual(out['error'], 'summary_json_invalid')
            self.assertEqual(out['validation']['reason'], 'invalid_escape')
            self.assertEqual(out['validation']['line'], 1)
            self.assertIsInstance(out['validation']['position'], int)
            self.assertNotIn('PRIVATE', json.dumps(out))
            self.assertNotIn('Users', json.dumps(out))
        self.assertEqual(self.cli('complete', '--batch', 'any', '--result-b64', '%%%')[1]['error'],
                         'summary_encoding_invalid')
        self.assertEqual(self.cli('complete', '--batch', 'any', '--result-json', '')[1]['error'],
                         'summary_json_invalid')

    def test_delivery_failure_retains_new_batch_cursor_and_real_error(self):
        self.complete([msg(1, reply='<alpha@test.local>')], {'alpha': 'ack'})
        self.gw.inject.side_effect = TimeoutError('PRIVATE TRANSPORT')
        code, out = self.cli('tick', '--effects', snapshot=snap([msg(2, reply='<gamma@test.local>')]))
        self.assertEqual(code, 0)
        self.assertEqual(out['status'], 'degraded')
        self.assertTrue(out['fire'])
        self.assertEqual(out['state']['cursor']['last_uid'], 2)
        self.assertEqual(out['batch']['topics'][0]['route_id'], 'gamma')
        self.assertEqual(out['dispatch'][0]['error'], 'effect_ack_unknown')
        self.assertNotIn('PRIVATE', json.dumps(out))
        unknown = self.db.db.execute("SELECT id FROM effects WHERE status='unknown'").fetchone()[0]
        self.gw.inject.side_effect = None
        self.gw.find.return_value = None
        # Pending destination proceeds, unknown destination is never blindly replayed.
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        calls = self.gw.inject.call_count
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.gw.inject.call_count, calls)
        self.assertEqual(self.db.db.execute('SELECT status FROM effects WHERE id=?', (unknown,)).fetchone()[0], 'unknown')
        self.gw.find.return_value = 'persisted-receipt'
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: DAY)['status'], 'reconciled')
        self.assertEqual(self.gw.inject.call_count, calls)
        self.assertEqual(self.db.db.execute('SELECT receipt FROM effects WHERE id=?', (unknown,)).fetchone()[0], 'persisted-receipt')

    def test_pre_intent_validation_failure_does_not_lose_batch_or_send(self):
        self.complete([msg(1, reply='<alpha@test.local>')], {'alpha': 'ack'})
        self.gw.validate.side_effect = m.MailError('route_validation_incomplete')
        code, out = self.cli('tick', '--effects', snapshot=snap([msg(2, reply='<gamma@test.local>')]))
        self.assertEqual(code, 0)
        self.assertTrue(out['fire'])
        self.assertEqual(out['status'], 'degraded')
        self.assertEqual(out['dispatch'][0]['error'], 'route_validation_incomplete')
        self.gw.inject.assert_not_called(); self.tg.send.assert_not_called()
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM effects WHERE status='pending'").fetchone()[0], 2)

    def test_unexpected_bug_is_not_hidden_as_success(self):
        with patch.object(m.Store, 'dispatch_one', side_effect=RuntimeError('PRIVATE')):
            code, out = self.cli('tick', '--effects')
        self.assertEqual(code, 1)
        self.assertEqual(out['error'], 'mail_runtime_failed')
        self.assertNotIn('PRIVATE', json.dumps(out))

    def test_positive_reconciliation_requires_injected_message_and_real_receipt(self):
        gw = m.Gateway(); target = 'agent:example:main'; marker = 'MAIL-synthetic-effect'
        message = {'role': 'assistant', 'model': 'gateway-injected', '__openclaw': {'id': 'persisted-id'},
                   'content': [{'type': 'text', 'text': 'context\n' + marker}]}
        with patch.object(gw, 'call', return_value={'sessionKey': target, 'messages': [message]}):
            self.assertEqual(gw.find(target, marker), 'persisted-id')
        for changed in (dict(message, role='user'), dict(message, model='ordinary-model'),
                        dict(message, __openclaw={}), dict(message, content=[{'type': 'text', 'text': marker + '-suffix'}])):
            with patch.object(gw, 'call', return_value={'sessionKey': target, 'messages': [changed]}):
                self.assertIsNone(gw.find(target, marker))
        with patch.object(gw, 'call', return_value={'sessionKey': 'other', 'messages': [message]}):
            with self.assertRaisesRegex(m.MailError, 'context_history_invalid'):
                gw.find(target, marker)
