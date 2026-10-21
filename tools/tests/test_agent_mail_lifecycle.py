"""Failure-oriented Seen and error-outbox proofs; no real transports."""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

import agent_mail_content as c
import agent_mail_seen as seen
import agent_mail_lifecycle as lifecycle
from test_agent_mail_content import RawIMAP, message
from test_agent_mail_router import m, msg, snap, result, DAY, NIGHT, MIDNIGHT


class FlagIMAP:
    def __init__(self, validity='77'):
        self.validity = validity
        self.flagset = {'\\Flagged', 'custom'}
        self.calls = []
        self.fail_ack = False
        self.confirm = True
        self.fetch_uid = '1'

    def select(self, mailbox, readonly=False):
        assert mailbox == '"INBOX"' and readonly is False
        self.calls.append(('SELECT', mailbox))
        return 'OK', []

    def response(self, name):
        return name, [self.validity.encode()]

    def uid(self, command, uid, fields, value=None):
        self.calls.append((command, uid, fields, value))
        assert uid == '1'  # No UID range, ALL, or sequence STORE is allowed.
        if command == 'STORE':
            assert fields == '+FLAGS.SILENT' and value == '(\\Seen)'
            if self.confirm:
                self.flagset.add('\\Seen')
            if self.fail_ack:
                self.fail_ack = False
                raise TimeoutError('PRIVATE_CREDENTIAL')
            return 'OK', []
        assert fields == '(UID FLAGS)'
        return 'OK', [f'1 (UID {self.fetch_uid} FLAGS ({" ".join(sorted(self.flagset))}))'.encode()]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.root = Path(self.tmp.name) / 'content'
        self.ledger = lifecycle.Ledger(self.state)
        self.tg = Mock()
        self.tg.send.return_value = {'status': 'sent', 'receipt': '42'}

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_error_dedup_restart_and_quiet_hours(self):
        eid = self.ledger.error('mail-monitor', 'login_failed')
        self.assertEqual(eid, self.ledger.error('mail-monitor', 'login_failed'))
        self.assertEqual(self.ledger.dispatch_error(self.tg, lambda: NIGHT)['status'], 'quiet')
        self.tg.send.assert_not_called()
        self.ledger.close(); self.ledger = lifecycle.Ledger(self.state)
        self.assertEqual(self.ledger.dispatch_error(self.tg, lambda: DAY)['status'], 'sent')
        route, text = self.tg.send.call_args.args
        self.assertEqual((route['chat_id'], route['thread_id']), ('900000001', '9001'))
        self.assertIn('Повторений: 2', text)
        self.ledger.error('mail-monitor', 'login_failed')
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 1)
        self.assertEqual(self.ledger.status()['errors'][0]['occurrences'], 3)

    def test_lost_telegram_ack_no_retry_and_other_error_proceeds(self):
        self.ledger.error('mail-monitor', 'poll_failed', now=1)
        self.tg.send.return_value = {'status': 'unknown', 'error': 'telegram_ack_unknown'}
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.ledger.close(); self.ledger = lifecycle.Ledger(self.state)
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 1)
        self.ledger.error('mail-content', 'decode_failed')
        self.tg.send.return_value = {'status': 'sent', 'receipt': '43'}
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 2)
        self.assertEqual(self.ledger.status()['error_counts'], [{'status': 'sent', 'count': 1}, {'status': 'unknown', 'count': 1}])

    def test_429_bounded_retry_and_last_gate_defer(self):
        self.ledger.error('mail-monitor', 'poll_failed')
        self.tg.send.return_value = {'status': 'pending', 'error': 'telegram_rejected'}
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.ledger.dispatch_error(self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 1)
        self.assertEqual(self.ledger.dispatch_error(self.tg, lambda: MIDNIGHT)['status'], 'quiet')

    def test_exception_scrub_and_distinct_scopes(self):
        eid = self.ledger.error('mail-content', 'SECRET token text', '<private@email>')
        other = self.ledger.error('mail-content', 'SECRET token text', 'different')
        self.assertNotEqual(eid, other)
        value = json.dumps(self.ledger.status())
        self.assertNotIn('SECRET', value); self.assertNotIn('private@email', value)
        self.assertIn('mail_operation_failed', value)

    def test_fallback_journal_replayed_exactly_once(self):
        with patch.object(lifecycle, 'Ledger', side_effect=sqlite3.OperationalError('disk error')):
            self.assertEqual(lifecycle.record_error(self.state, 'mail-monitor', 'mail_db_failed'), 'fallback_journal')
        self.ledger.close(); self.ledger = lifecycle.Ledger(self.state)
        self.assertEqual(self.ledger.status()['errors'][0]['occurrences'], 1)
        self.ledger.close(); self.ledger = lifecycle.Ledger(self.state)
        self.assertEqual(self.ledger.status()['errors'][0]['occurrences'], 1)
        self.assertTrue((self.state / 'errors-fallback.jsonl').exists())

    def test_read_ranges_holes_overlaps_and_hash_version(self):
        self.ledger.note_read('77:1', 'raw', '1', 'text', 100, 50, 100)
        self.assertFalse(self.ledger.fully_read('77:1', 'raw', '1', 'text', 100))
        self.ledger.note_read('77:1', 'raw', '1', 'text', 100, 0, 49)
        self.assertFalse(self.ledger.fully_read('77:1', 'raw', '1', 'text', 100))
        self.ledger.note_read('77:1', 'raw', '1', 'text', 100, 48, 51)
        self.assertTrue(self.ledger.fully_read('77:1', 'raw', '1', 'text', 100))
        self.assertFalse(self.ledger.fully_read('77:1', 'newraw', '1', 'text', 100))
        self.assertFalse(self.ledger.fully_read('77:1', 'raw', '1', 'newtext', 100))

    def test_router_poll_failure_persists_and_dispatches_without_successful_poll(self):
        routes = Path(self.tmp.name) / 'routes.json'
        routes.write_text(json.dumps({'version': 1, 'routes': []}))
        clock = Mock(); clock.now.return_value = DAY
        with patch.object(m, 'poll', side_effect=m.MailError('mail_snapshot_failed')), patch.object(m, 'Telegram', return_value=self.tg), patch.object(lifecycle, 'datetime', clock), redirect_stdout(io.StringIO()) as stdout:
            code = m.main(['--state-dir', str(self.state), '--routes', str(routes), 'tick', '--effects',
                           '--trigger-state-json', '{"cursor":{"uidvalidity":"77","last_uid":0}}'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())['dispatch']['status'], 'sent')
        self.assertEqual(self.tg.send.call_count, 1)
        self.assertEqual(self.ledger.status()['errors'][0]['code'], 'mail_snapshot_failed')

    def test_bad_routes_do_not_block_independent_error_queue(self):
        bad = Path(self.tmp.name) / 'bad.json'; bad.write_text('{}')
        with redirect_stdout(io.StringIO()), patch.object(m, 'Telegram', return_value=self.tg):
            self.assertEqual(m.main(['--state-dir', str(self.state), '--routes', str(bad), 'tick']), 1)
        self.assertEqual(self.ledger.status()['errors'][0]['status'], 'pending')
        with redirect_stdout(io.StringIO()), patch.object(m, 'Telegram', return_value=self.tg):
            self.assertEqual(m.main(['--state-dir', str(self.state), '--routes', str(bad), 'errors-status']), 0)


class SeenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'; self.root = Path(self.tmp.name) / 'content'
        self.route = {'id': 'general', 'parent': lifecycle.MAIL_TOPIC['parent'], 'title': 'mail', 'fallback': True,
                      'chat_id': '900000001', 'thread_id': '9001'}
        self.store = m.Store(self.state / 'mail.sqlite3', {'general': self.route})
        self.store.bootstrap({'cursor': {'uidvalidity': '77', 'last_uid': 0}})
        self.ledger = lifecycle.Ledger(self.state)
        self.meta = dict(msg(1), content_request=c.request_for('77', 1, c.SENDERS[0], '<m1@test.local>'))

    def tearDown(self):
        self.store.db.close(); self.ledger.close(); self.tmp.cleanup()

    def admit(self, mail=None, legacy=False, complete=True):
        mail = mail or message('actually read body')
        if legacy:
            self.meta.pop('content_request', None)
            self.meta.update(body='actually read body', body_status='plain', body_truncated=False)
        self.store.ingest(snap([self.meta]))
        self.manifest = c.acquire(RawIMAP(mail.as_bytes()), self.meta, self.root)
        if complete:
            with patch.object(m, 'CONTENT_ROOT', self.root):
                batch = self.store.batch(); self.store.complete(batch['batch_id'], [result('general')])
        return self.manifest

    def read_all(self):
        for p in self.manifest['parts']:
            if p.get('text'):
                total = len(Path(p['text']['path']).read_text())
                self.ledger.note_read(self.meta['key'], self.manifest['original']['sha256'], p['id'], p['text']['sha256'], total, 0, total)

    def proof(self):
        return seen.proof_for(self.state, self.root, self.meta, self.ledger)

    def test_no_seen_on_acquisition_or_missing_summary_or_unread(self):
        self.admit(complete=False)
        self.read_all()
        with self.assertRaisesRegex(seen.SeenError, 'accepted_summary_required'):
            self.proof()
        with patch.object(m, 'CONTENT_ROOT', self.root):
            batch = self.store.batch(); self.store.complete(batch['batch_id'], [result('general')])
        self.assertEqual(self.ledger.status()['seen'], [])
        self.ledger.db.execute('DELETE FROM reads'); self.ledger.db.commit()
        with self.assertRaisesRegex(seen.SeenError, 'full_read_evidence_required'):
            self.proof()

    def test_full_reads_summary_then_exact_uid_additive_store_and_receipt(self):
        self.admit(); self.read_all(); proof = self.proof(); client = FlagIMAP()
        out = seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertEqual(client.flagset, {'\\Seen', '\\Flagged', 'custom'})
        self.assertEqual(out['before'], ['\\Flagged', 'custom'])
        self.assertEqual(self.ledger.status()['seen'][0]['status'], 'confirmed')
        seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertEqual(len([x for x in client.calls if x[0] == 'STORE']), 1)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM seen_attempts').fetchone()[0], 2)

    def test_uidvalidity_reset_and_mismatched_fetch_prevent_store(self):
        self.admit(); self.read_all(); proof = self.proof()
        client = FlagIMAP('78')
        with self.assertRaisesRegex(seen.SeenError, 'seen_uidvalidity_reset'):
            seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertFalse(any(x[0] == 'STORE' for x in client.calls))
        client = FlagIMAP(); client.fetch_uid = '2'
        with self.assertRaisesRegex(Exception, 'fetch_uid_missing_or_mismatched'):
            seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertFalse(any(x[0] == 'STORE' for x in client.calls))

    def test_lost_imap_ack_safe_retry_verifies_without_second_store(self):
        self.admit(); self.read_all(); proof = self.proof(); client = FlagIMAP(); client.fail_ack = True
        with self.assertRaises(TimeoutError):
            seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertEqual(self.ledger.status()['seen'][0]['status'], 'pending')
        self.assertNotIn('PRIVATE_CREDENTIAL', json.dumps(self.ledger.status()))
        seen.apply_seen(client, self.meta, proof, self.ledger)
        self.assertEqual(len([x for x in client.calls if x[0] == 'STORE']), 1)
        self.assertEqual(self.ledger.status()['seen'][0]['status'], 'confirmed')

    def test_store_without_observed_seen_remains_pending_error(self):
        self.admit(); self.read_all(); client = FlagIMAP(); client.confirm = False
        with self.assertRaisesRegex(seen.SeenError, 'seen_not_confirmed'):
            seen.apply_seen(client, self.meta, self.proof(), self.ledger)
        self.assertEqual(self.ledger.status()['seen'][0]['error'], 'seen_not_confirmed')
        self.assertEqual(self.ledger.status()['errors'][0]['code'], 'seen_not_confirmed')

    def test_legacy_backfill_requires_exact_complete_single_part(self):
        self.admit(legacy=True)
        self.assertTrue(self.proof()['basis'].startswith('legacy_complete'))
        self.meta['body_truncated'] = True
        self.store.db.execute('UPDATE mail SET payload=?', (m.canonical(self.meta),)); self.store.db.commit()
        with self.assertRaisesRegex(seen.SeenError, 'full_read_evidence_required'):
            self.proof()

    def test_attachment_pending_even_if_body_read_does_not_mark_seen(self):
        mail = message('actually read body'); mail.add_attachment(b'pdf bytes', maintype='application', subtype='pdf', filename='a.pdf')
        self.admit(mail); self.read_all()
        with self.assertRaisesRegex(seen.SeenError, 'all_parts_must_be_readable'):
            self.proof()
        self.assertEqual(self.ledger.status()['seen'], [])

    def test_original_and_text_tampering_fail_closed(self):
        self.admit(); self.read_all()
        Path(self.manifest['parts'][0]['text']['path']).write_text('tampered')
        with self.assertRaisesRegex(seen.SeenError, 'text_integrity_failed'):
            self.proof()
        Path(self.manifest['original']['path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(seen.SeenError, 'original_integrity_failed'):
            self.proof()

    def test_unknown_is_never_eligible(self):
        self.meta['sender'] = 'unknown@example.invalid'
        with self.assertRaisesRegex(seen.SeenError, 'unknown_sender_not_read'):
            self.proof()

    def test_broken_read_output_never_creates_read_evidence_or_seen_intent(self):
        self.admit()
        with patch('builtins.print', side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                c.main(['--state-dir', str(self.state), '--content-root', str(self.root), 'read', '--key', '77:1', '--part', '1'])
        self.assertEqual(self.ledger.status()['seen'], [])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM reads').fetchone()[0], 0)

    def test_unready_cli_seen_has_no_transport_or_error_alert(self):
        self.admit()
        with patch.object(c.imaplib, 'IMAP4_SSL', side_effect=AssertionError('network forbidden')), redirect_stdout(io.StringIO()) as output:
            code = c.main(['--state-dir', str(self.state), '--content-root', str(self.root), 'seen', '--key', '77:1', '--apply'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]['status'], 'not_ready')
        self.assertEqual(self.ledger.status()['errors'], [])

    def test_complete_cli_queues_proven_intent_and_tick_effects_services_exact_key(self):
        self.admit(); self.read_all()
        bid = self.store.db.execute('SELECT id FROM batches').fetchone()[0]
        routes = Path(self.tmp.name) / 'routes.json'
        routes.write_text(json.dumps({'version': 1, 'routes': [dict(self.route, evidence='test fixture')]}))
        prefix = ['--state-dir', str(self.state), '--routes', str(routes)]
        with patch.object(m, 'CONTENT_ROOT', self.root), redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(prefix + ['complete', '--batch', bid, '--result-json', json.dumps([result('general')])]), 0)
        self.assertEqual(self.ledger.status()['seen'][0]['status'], 'pending')
        gw = Mock(); gw.inject.return_value = 'synthetic-context'; gw.find.return_value = None
        tg = Mock(); tg.send.return_value = {'status': 'sent', 'receipt': 'synthetic-notify'}
        with patch.object(m, 'poll', return_value=snap([], last=1)), patch.object(m, 'Gateway', return_value=gw), patch.object(m, 'Telegram', return_value=tg), patch.object(m, 'run_json', return_value=[{'status': 'confirmed'}]) as worker, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(prefix + ['tick', '--effects']), 0)
        argv = worker.call_args.args[0]
        self.assertIn(str(self.state), argv)
        self.assertEqual(argv[-4:], ['seen', '--key', '77:1', '--apply'])
        self.assertEqual(worker.call_args.kwargs['timeout'], 10)

    def test_real_cli_read_records_coverage_and_queues_after_prior_summary(self):
        self.admit()
        with redirect_stdout(io.StringIO()) as output:
            code = c.main(['--state-dir', str(self.state), '--content-root', str(self.root), 'read', '--key', '77:1', '--part', '1'])
        self.assertEqual(code, 0)
        self.assertIn('actually read body', json.loads(output.getvalue())['untrusted_text'])
        self.assertEqual(self.ledger.status()['seen'][0]['status'], 'pending')
        self.assertEqual(self.proof()['basis'], 'full_range_reads_and_accepted_summary')


if __name__ == '__main__':
    unittest.main()
