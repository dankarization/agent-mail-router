"""Deferred pipeline proof. IMAP/delivery are fakes; output lives in test roots."""
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import syncopia_agent_mail_content as c
import syncopia_agent_mail_snapshot as snapshot
from test_syncopia_agent_mail_snapshot import FakeIMAP, PLAIN
import test_syncopia_agent_mail_router as router_tests
from test_syncopia_agent_mail_router import msg, snap


class RawIMAP:
    def __init__(self, raw, validity='77'):
        self.raw, self.validity, self.calls = raw, validity, []
        self.fail = False

    def select(self, mailbox, readonly=False):
        assert mailbox == '"INBOX"' and readonly is True
        self.calls.append(('EXAMINE',))
        return 'OK', []

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [self.validity.encode()]

    def uid(self, command, uid, fields):
        assert command == 'FETCH'
        self.calls.append((command, uid, fields))
        if self.fail and 'BODY.PEEK' in fields:
            raise OSError('PRIVATE_ERROR_MUST_NOT_LEAK')
        if fields == '(UID RFC822.SIZE)':
            return 'OK', [f'1 (UID {uid} RFC822.SIZE {len(self.raw)})'.encode()]
        match = re.fullmatch(r'\(UID BODY.PEEK\[\]<(\d+)\.(\d+)>\)', fields)
        assert match, fields
        offset, size = map(int, match.groups())
        raw = self.raw[offset:offset + size]
        return 'OK', [(f'1 (UID {uid} BODY[]<{offset}> {{{len(raw)}}}'.encode(), raw), b')']


def message(text='hello', sender=c.SENDERS[0]):
    mail = EmailMessage()
    mail['From'] = sender
    mail['To'] = c.ACCOUNT
    mail['Message-ID'] = '<m1@test.local>'
    mail['Subject'] = 'Synthetic test'
    mail.set_content(text)
    return mail


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.meta = dict(msg(1), content_request=c.request_for('77', 1, c.SENDERS[0], '<m1@test.local>'))

    def tearDown(self):
        self.tmp.cleanup()

    def acquire(self, mail):
        self.raw = mail.as_bytes()
        self.client = RawIMAP(self.raw)
        return c.acquire(self.client, self.meta, self.root)

    def test_long_unicode_no_old_truncation_and_bounded_reads(self):
        text = ('Полный текст ' * 20000) + 'FINAL_SENTINEL'
        manifest = self.acquire(message(text))
        body = manifest['parts'][0]
        self.assertEqual(manifest['original']['sha256'], hashlib.sha256(self.raw).hexdigest())
        offset, chunks = 0, []
        while True:
            page = c.read_text(self.root, self.meta, body['id'], offset)
            self.assertLessEqual(len(page['untrusted_text']), c.READ_CHARS)
            chunks.append(page['untrusted_text'])
            offset = page['next_offset']
            if not page['has_more']:
                break
        self.assertEqual(''.join(chunks).rstrip(), text)
        self.assertGreater(len(self.client.calls), 3)
        self.assertEqual(manifest['analysis_status'], 'not_read')

    def test_three_exact_senders_and_unverified_auth_headers(self):
        for sender in c.SENDERS:
            with self.subTest(sender=sender):
                client = FakeIMAP()
                client.messages[1]['sender'] = sender
                out = snapshot.snapshot(client)[0]['messages'][0]
                self.assertEqual(out['trust_class'], 'known_from')
                self.assertEqual(out['sender'], sender)
                self.assertFalse(out['authentication']['verified'])
        client.messages[1]['headers'] = (
            f'From: {sender}\r\nAuthentication-Results: forged; dkim=pass\r\nReceived-SPF: pass\r\n\r\n').encode()
        out = snapshot.snapshot(client)[0]['messages'][0]
        self.assertFalse(out['authentication']['verified'])
        self.assertIn('dkim=pass', out['authentication']['authentication_results'][0])

    def test_html_only_strips_active_content_and_no_remote_fetch(self):
        mail = message()
        mail.set_content('<html><head>HIDDEN</head><body><p>visible</p><script>EVIL</script><img src="http://evil/x"></body></html>', subtype='html')
        out = self.acquire(mail)
        read = c.read_text(self.root, self.meta, out['parts'][0]['id'])
        self.assertIn('visible', read['untrusted_text'])
        self.assertNotIn('EVIL', read['untrusted_text'])
        self.assertNotIn('HIDDEN', read['untrusted_text'])

    def test_cid_pdf_docx_attribution_and_tech_docs_roundtrip(self):
        mail = message()
        mail.add_alternative('<p>text<img src="cid:scan"></p>', subtype='html')
        mail.get_payload()[1].add_related(b'not-a-real-png', maintype='image', subtype='png', cid='<scan>')
        mail.add_attachment(b'%PDF-fake', maintype='application', subtype='pdf', filename='../../secret.pdf')
        mail.add_attachment(b'PK fake', maintype='application', subtype='vnd.openxmlformats-officedocument.wordprocessingml.document', filename='notes.docx')
        manifest = self.acquire(mail)
        parts = [p for p in manifest['parts'] if p['role'] == 'attachment']
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(p['state'] == 'pending' and p['analysis_status'] == 'not_read' for p in parts))
        self.assertTrue(all(Path(p['file']['path']).is_relative_to(self.root) for p in parts))
        self.assertTrue(any(p['content_id'] == '<scan>' for p in parts))
        handoff = c.handoff(manifest, self.root, self.meta)
        self.assertEqual(handoff['owner'], 'tech-docs')
        self.assertEqual(len(handoff['inputs']), 3)
        output = Path(handoff['output_dir'])
        for folder in ('clean', 'sections', 'page_map'):
            (output / folder).mkdir(parents=True, exist_ok=True)
        (output / 'clean/one.txt').write_text('Synthetic tech-docs returned text\n')
        (output / 'sections/one.jsonl').write_text('{"source_ref":"page 1"}\n')
        (output / 'page_map/one.jsonl').write_text('{"page":1}\n')
        row = {'id': parts[0]['id'], 'sha256': parts[0]['file']['sha256'], 'state': 'extracted',
               'method': 'synthetic-tech-docs-test-double; NOT real OCR', 'quality': 'synthetic', 'warnings': ['fixture'],
               'text_path': 'clean/one.txt', 'sections_path': 'sections/one.jsonl', 'page_map_path': 'page_map/one.jsonl'}
        result = {'key': self.meta['key'], 'source_sha256': manifest['original']['sha256'], 'parts': [row]}
        c.accept_result(self.root, self.meta, result)
        self.assertIn('returned text', c.read_text(self.root, self.meta, row['id'])['untrusted_text'])
        self.assertEqual(c.accept_result(self.root, self.meta, result), c.read_manifest(self.root, self.meta))
        row['text_path'] = '../../../../etc/passwd'
        with self.assertRaisesRegex(c.ContentError, 'extraction_output_invalid'):
            c.accept_result(self.root, self.meta, result)

    def test_unknown_not_promoted_by_display_name_or_review(self):
        self.meta.update(sender='attacker@example.com', trust_class='unknown')
        self.meta['content_request'] = c.source_for(self.meta)
        raw = message(sender='Source A <attacker@example.com>').as_bytes()
        client = RawIMAP(raw)
        out = c.acquire(client, self.meta, self.root)
        self.assertEqual(out['state'], 'awaiting_review')
        self.assertEqual(client.calls, [])
        out = c.acquire(client, self.meta, self.root, include_unknown=True)
        self.assertIn('original', out)
        self.assertNotIn('parts', out)
        with self.assertRaisesRegex(c.ContentError, 'unknown_sender_review_only'):
            c.accept_result(self.root, self.meta, {})

    def test_restart_preserves_original_and_does_not_refetch(self):
        out = self.acquire(message())
        self.client.calls.clear()
        restarted = c.acquire(self.client, self.meta, self.root)
        self.assertEqual(restarted, out)
        self.assertEqual(self.client.calls, [])
        Path(out['original']['path']).write_bytes(b'corruption')
        with self.assertRaisesRegex(c.ContentError, 'original_hash_mismatch'):
            c.acquire(self.client, self.meta, self.root)

    def test_oversize_visible_no_body_fetch_original_locator_retained(self):
        with patch.object(c, 'MAX_RAW', 10):
            out = self.acquire(message())
        self.assertEqual(out['state'], 'limit')
        self.assertTrue(out['remote_original_available'])
        self.assertEqual(len(self.client.calls), 2)
        self.assertFalse((self.root / c.identity(self.meta) / 'original.eml').exists())

    def test_unsupported_and_part_limits_keep_original(self):
        mail = message()
        mail.add_attachment(b'zip bytes', maintype='application', subtype='zip', filename='never-unpack.zip')
        out = self.acquire(mail)
        self.assertEqual(out['parts'][-1]['state'], 'unsupported')
        self.assertTrue(Path(out['original']['path']).exists())
        with tempfile.TemporaryDirectory() as target, patch.object(c, 'MAX_PART', 1):
            out = c.acquire(RawIMAP(mail.as_bytes()), self.meta, Path(target))
            self.assertTrue(all(p['state'] == 'limit' for p in out['parts']))
            self.assertTrue(Path(out['original']['path']).exists())

    def test_interrupted_download_and_uid_reset_no_false_available(self):
        client = RawIMAP(message().as_bytes())
        client.fail = True
        with self.assertRaises(OSError):
            c.acquire(client, self.meta, self.root)
        self.assertFalse((self.root / c.identity(self.meta) / 'original.eml').exists())
        client.fail = False
        self.assertIn('original', c.acquire(client, self.meta, self.root))
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaisesRegex(c.ContentError, 'uidvalidity_reset'):
                c.acquire(RawIMAP(message().as_bytes(), validity='88'), self.meta, Path(target))

    def test_malformed_original_retained_and_warnings_not_hidden(self):
        raw = (f'From: {c.SENDERS[0]}\nMessage-ID: <m1@test.local>\n'
               'Content-Type: multipart/mixed; boundary=missing\n\nMalformed').encode()
        out = c.acquire(RawIMAP(raw), self.meta, self.root)
        self.assertTrue(Path(out['original']['path']).exists())
        self.assertIn('StartBoundaryNotFoundDefect', out['warnings'])
        self.assertNotEqual(out['state'], 'available')
        client = FakeIMAP()
        client.messages[1]['structure'] = b'(garbage)'
        out = snapshot.snapshot(client)[0]['messages'][0]
        self.assertEqual(out['body_status'], 'error_pending_full_read')
        self.assertEqual(out['content_request']['state'], 'pending')

    def test_store_limit_and_wrong_from_keep_explicit_failure(self):
        with patch.object(c, 'MAX_STORE', 1):
            out = self.acquire(message())
        self.assertEqual(out['state'], 'limit')
        self.assertEqual(out['reason'], 'store_limit')
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaisesRegex(c.ContentError, 'download_identity_mismatch'):
                c.acquire(RawIMAP(message(sender=c.SENDERS[1]).as_bytes()), self.meta, Path(target))
            self.assertTrue((Path(target) / c.identity(self.meta) / 'original.eml').exists())

    def test_hash_bound_extraction_and_read_integrity(self):
        out = self.acquire(message())
        with self.assertRaisesRegex(c.ContentError, 'extraction_source_mismatch'):
            c.accept_result(self.root, self.meta, {'key': self.meta['key'], 'source_sha256': 'wrong'})
        Path(out['parts'][0]['text']['path']).write_text('tampered')
        with self.assertRaisesRegex(c.ContentError, 'text_integrity_failed'):
            c.read_text(self.root, self.meta, out['parts'][0]['id'])


class ExtendedRoutingTests(unittest.TestCase):
    setUp = router_tests.RoutingTests.setUp
    tearDown = router_tests.RoutingTests.tearDown
    complete = router_tests.RoutingTests.complete

    def test_batch_preview_bounded_without_losing_durable_payload(self):
        original = msg(1, 'x' * 16000, reply='<alpha@test.local>')
        original['content_request'] = c.source_for(original)
        with patch.object(router_tests.m, 'CONTENT_ROOT', Path(self.tmp.name) / 'content'):
            self.db.ingest(snap([original]))
            batch = self.db.batch()
        projected = batch['topics'][0]['messages'][0]
        self.assertEqual(len(projected['body']), 4000)
        self.assertTrue(projected['body_truncated'])
        self.assertEqual(projected['reading']['state'], 'pending')
        saved = json.loads(self.db.db.execute('SELECT payload FROM mail').fetchone()[0])
        self.assertEqual(len(saved['body']), 16000)

    def test_render_includes_runtime_attachment_states_not_false_read_claim(self):
        from unittest.mock import patch
        source = dict(msg(1, reply='<alpha@test.local>'), content_request=c.request_for('77', 1, c.SENDERS[0], '<m1@test.local>'))
        manifest = {'state': 'available', 'parts': [{'id': '1.1', 'state': 'pending', 'role': 'attachment', 'mime': 'image/png'},
                                                 {'id': '1.2', 'state': 'unsupported', 'role': 'attachment', 'mime': 'application/zip'}]}
        with patch.object(router_tests.m, 'read_manifest', return_value=manifest):
            self.complete([source])
        summary = json.loads(self.db.db.execute('SELECT payload FROM summaries').fetchone()[0])
        rendered = self.db.render(summary, 'marker', telegram=True)
        self.assertIn('ожидает разбора=1', rendered)
        self.assertIn('unsupported=1', rendered)
        self.assertIn('Полное прочтение не подтверждено', rendered)

    def test_unknown_durable_triage_no_batch_anchors_or_effects(self):
        unknown = dict(msg(1, '[EXAMPLE-GAMMA]', reply='<alpha@test.local>'), sender='attacker@example.com', trust_class='unknown')
        unknown['content_request'] = c.source_for(unknown)
        self.db.ingest(snap([unknown]))
        self.db.reroute()
        self.assertIsNone(self.db.batch())
        self.assertEqual(self.db.status()['triage'][0]['reason'], 'unknown_sender_review')
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM effects').fetchone()[0], 0)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM anchors WHERE id=?', (unknown['message_id'],)).fetchone()[0], 0)

    def test_new_primary_routed_and_labeled_correctly(self):
        primary = dict(msg(1, reply='<alpha@test.local>'), sender=c.PRIMARY_SENDERS[1])
        self.complete([primary])
        summary = json.loads(self.db.db.execute('SELECT payload FROM summaries').fetchone()[0])
        self.assertIn('Source A:', self.db.render(summary, 'marker'))
        self.assertNotIn('Source B:', self.db.render(summary, 'marker'))

    def test_same_id_different_uid_unresolved_content_is_collision(self):
        first = msg(1, reply='<alpha@test.local>')
        first['content_request'] = c.source_for(first)
        self.db.ingest(snap([first]))
        second = dict(first, key='77:2', uid=2)
        second['content_request'] = c.source_for(second)
        self.db.ingest(snap([second]))
        self.assertEqual(self.db.db.execute('SELECT reason FROM mail WHERE key=?', ('77:2',)).fetchone()[0], 'message_id_collision')
