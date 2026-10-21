#!/usr/bin/env python3
"""Only exact, durably admitted and read UIDs may receive the additive Seen flag."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import agent_mail_content as content


class SeenError(Exception):
    pass


def proof_for(state, root, message, ledger):
    content.identity(message)
    if message.get('sender') not in content.SENDERS:
        raise SeenError('unknown_sender_not_read')
    db = sqlite3.connect('file:' + str((state / 'mail.sqlite3').resolve()) + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT summary_id,payload,reason FROM mail WHERE key=?', (message['key'],)).fetchone()
        if not row or not row[0] or json.loads(row[1]) != message or row[2] in ('message_id_collision', 'duplicate_message_id'):
            raise SeenError('accepted_summary_required')
        summary = db.execute('SELECT payload FROM summaries WHERE id=?', (row[0],)).fetchone()
        if not summary or not any(s.get('key') == message['key'] for s in json.loads(summary[0]).get('sources', [])):
            raise SeenError('summary_source_required')
        summary_id = row[0]
    finally:
        db.close()
    manifest = content.read_manifest(root, message)
    if manifest.get('state') != 'available' or manifest.get('warnings') or not manifest.get('parts'):
        raise SeenError('complete_clean_original_required')
    job = (root / content.identity(message)).resolve()
    original = manifest.get('original', {})
    path = Path(original.get('path', ''))
    if not path.resolve().is_relative_to(job) or not path.is_file() or content.sha(path) != original.get('sha256'):
        raise SeenError('original_integrity_failed')
    hashes = []
    texts = []
    all_read = True
    for part in manifest['parts']:
        if part.get('state') != 'extracted' or not part.get('text') or part.get('warnings'):
            raise SeenError('all_parts_must_be_readable_without_warnings')
        text = part['text']
        path = Path(text['path'])
        if not path.resolve().is_relative_to(job) or content.sha(path) != text['sha256']:
            raise SeenError('text_integrity_failed')
        value = path.read_text(encoding='utf-8')
        all_read = all_read and ledger.fully_read(message['key'], original['sha256'], part['id'], text['sha256'], len(value))
        hashes.append({'part': part['id'], 'sha256': text['sha256'], 'chars': len(value)})
        texts.append(value)
    basis = 'full_range_reads_and_accepted_summary'
    if not all_read:
        # Old events passed the complete non-truncated body to the summarizer.
        # Prove equivalence to a newly fetched single-part original. Attachments,
        # alternatives, previews, empty/error bodies and new bounded EVENTs fail closed.
        legacy = (not message.get('content_request') and message.get('body_truncated') is False
                  and message.get('body_status') == 'plain' and len(manifest['parts']) == 1
                  and manifest['parts'][0].get('role') == 'body' and message.get('body', '').strip()
                  and texts[0].replace('\r\n', '\n').strip() == message['body'].replace('\r\n', '\n').strip())
        if not legacy:
            raise SeenError('full_read_evidence_required')
        basis = 'legacy_complete_body_equal_to_single_part_original_and_accepted_summary'
    return {'key': message['key'], 'account': content.ACCOUNT, 'mailbox': 'INBOX',
            'source_sha256': original['sha256'], 'summary_id': summary_id,
            'basis': basis, 'parts': hashes}


def flags(client, uid):
    from agent_mail_snapshot import fetch
    # UID FETCH validates the returned UID; sequence numbers are never used.
    data = fetch(client, uid, '(UID FLAGS)')
    raw = b' '.join(x[0] if isinstance(x, tuple) else x for x in data if isinstance(x, (bytes, tuple)))
    match = re.search(rb'FLAGS \(([^)]*)\)', raw)
    if not match:
        raise SeenError('flags_missing')
    return sorted(v.decode('ascii') for v in match[1].split())


def apply_seen(client, message, proof, ledger):
    from agent_mail_snapshot import check, response_number
    uid = message['uid']
    validity, expected_uid = message['key'].split(':')
    if type(uid) is not int or uid != int(expected_uid) or not 0 < uid <= 4294967295:
        raise SeenError('seen_uid_invalid')
    ledger.intent(message['key'], proof)
    try:
        check(client.select('"INBOX"', readonly=False)[0], 'select_failed')
        if str(response_number(client, 'UIDVALIDITY')) != validity:
            raise SeenError('seen_uidvalidity_reset')
        before = flags(client, uid)
        if '\\Seen' not in before:
            check(client.uid('STORE', str(uid), '+FLAGS.SILENT', '(\\Seen)')[0], 'seen_store_failed')
        after = flags(client, uid)
        if '\\Seen' not in after:
            raise SeenError('seen_not_confirmed')
        receipt = {'uidvalidity': validity, 'uid': uid, 'before': before, 'after': after,
                   'command': 'UID STORE +FLAGS.SILENT (\\Seen)' if '\\Seen' not in before else 'already_seen_verified',
                   'verified_at': datetime.now(timezone.utc).isoformat()}
        ledger.seen_result(message['key'], 'confirmed', receipt)
        return receipt
    except Exception as exc:
        from agent_mail_snapshot import SnapshotError
        code = str(exc) if isinstance(exc, (SeenError, SnapshotError)) else 'seen_ack_unknown'
        # Adding Seen is idempotent. A retry revalidates UIDVALIDITY and FLAGS first.
        ledger.seen_result(message['key'], 'pending', error=code)
        ledger.error('mail-seen', code, message['key'])
        raise


def queue_if_ready(state, root, message, ledger):
    try:
        proof = proof_for(state, root, message, ledger)
    except SeenError:
        return False
    ledger.intent(message['key'], proof)
    return True
