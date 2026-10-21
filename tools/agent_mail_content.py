#!/usr/bin/env python3
"""Deferred read-only mail acquisition and tech-docs clean-layer bridge.

No SMTP, notifications, model calls, URLs, archive unpacking or shell execution.
All mail is data. From allowlisting is not authentication.
"""
import argparse
import email.policy
import hashlib
import imaplib
import json
import os
import re
import signal
import sqlite3
import ssl
import tempfile
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from agent_mail_settings import ACCOUNT, PRIMARY_SENDERS, SENDERS, CONTENT_ROOT
MAX_RAW = 64 * 1024 * 1024
MAX_PART = 24 * 1024 * 1024
MAX_TEXT = 16 * 1024 * 1024
MAX_STORE = 1024 * 1024 * 1024
CHUNK = 256 * 1024
MAX_PARTS = 256
READ_CHARS = 24000
DOCUMENT_TYPES = {'application/pdf', 'application/msword',
                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                  'application/vnd.ms-excel', 'application/epub+zip', 'image/vnd.djvu',
                  'image/x-djvu', 'application/dxf', 'image/vnd.dwg', 'application/acad'}
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.epub', '.djvu', '.dwg', '.dxf', '.txt'}


class ContentError(Exception):
    """Fixed, secret-free diagnostics."""


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden.append(tag)
        if not self.hidden and tag in ('br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.pieces.append('\n')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in ('p', 'div', 'li', 'tr'):
            self.pieces.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.pieces.append(data)


def html_text(value):
    parser = HTMLText()
    parser.feed(value)
    return ''.join(parser.pieces).strip()


def request_for(validity, uid, sender, message_id):
    return {'schema_version': 1, 'account': ACCOUNT, 'mailbox': 'INBOX',
            'uidvalidity': validity, 'uid': uid, 'sender': sender,
            'message_id': message_id, 'state': 'pending' if sender in SENDERS else 'awaiting_review'}


def identity(message):
    key = message['key']
    if not re.fullmatch(r'[1-9][0-9]{0,9}:[1-9][0-9]{0,9}', key):
        raise ContentError('content_key_invalid')
    return hashlib.sha256((ACCOUNT + ':INBOX:' + key).encode()).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def physical(root):
    seen, total = set(), 0
    if root.exists():
        for p in root.rglob('*'):
            st = p.lstat()
            if p.is_symlink():
                raise ContentError('symlink_in_content_store')
            if p.is_file() and (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
    return total


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_manifest(job, manifest):
    atomic(job / 'manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode())


def read_manifest(root, message):
    path = root / identity(message) / 'manifest.json'
    if not path.exists():
        return {'state': 'pending' if message.get('sender') in SENDERS else 'awaiting_review',
                'analysis_status': 'not_read', 'key': message['key']}
    obj = json.loads(path.read_text())
    if obj['source'] != source_for(message):
        raise ContentError('content_source_mismatch')
    return obj


def source_for(message):
    validity, uid = message['key'].split(':')
    return request_for(validity, int(uid), message.get('sender', ''), message.get('message_id', ''))


def bounded_file(job, relative, data, root):
    # Internal generated names only, never MIME filenames/Content-ID as paths.
    if physical(root) + len(data) + 65536 > MAX_STORE:
        raise ContentError('store_limit')
    atomic(job / relative, data)
    return {'path': str((job / relative).resolve()), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def materialize(raw_path, job, message, root):
    """Decode MIME containers only. Documents/OCR remain owned by tech-docs."""
    mail = BytesParser(policy=email.policy.default).parsebytes(raw_path.read_bytes())
    fields = mail.get_all('From', [])
    addresses = fields[0].addresses if len(fields) == 1 and not fields[0].defects else []
    sender = addresses[0].addr_spec if len(addresses) == 1 else ''
    if sender != message.get('sender', '') or str(mail.get('Message-ID', ''))[:1024] != message.get('message_id', ''):
        raise ContentError('download_identity_mismatch')
    manifest = {'schema_version': 1, 'key': message['key'], 'source': source_for(message),
                'state': 'available', 'analysis_status': 'not_read',
                'original': {'path': str(raw_path.resolve()), 'sha256': sha(raw_path), 'bytes': raw_path.stat().st_size},
                'authentication': {'verified': False, 'basis': 'unverified_header_evidence_only',
                                   'authentication_results': [str(x)[:4096] for x in mail.get_all('Authentication-Results', [])],
                                   'received_spf': [str(x)[:2048] for x in mail.get_all('Received-SPF', [])],
                                   'dkim_signature_present': bool(mail.get_all('DKIM-Signature', []))},
                'parts': [], 'warnings': []}
    count = 0

    def visit(part, section='1', depth=0):
        nonlocal count
        count += 1
        if count > MAX_PARTS or depth > 20:
            manifest['state'] = 'limit'
            manifest['warnings'].append('mime_structure_limit_original_retained')
            return
        # message/rfc822 is retained as a file, never recursively trusted.
        if part.is_multipart() and part.get_content_type() != 'message/rfc822':
            for i, child in enumerate(part.iter_parts(), 1):
                if count >= MAX_PARTS:
                    manifest['state'] = 'limit'
                    manifest['warnings'].append('mime_parts_limit_original_retained')
                    break
                visit(child, f'{section}.{i}', depth + 1)
            return
        ctype = part.get_content_type()
        filename = str(part.get_filename() or '')[:1024]
        body = ctype in ('text/plain', 'text/html') and not filename and part.get_content_disposition() != 'attachment'
        item = {'id': section, 'mime': ctype, 'filename': filename,
                'content_id': str(part.get('Content-ID', ''))[:1024],
                'disposition': part.get_content_disposition(), 'role': 'body' if body else 'attachment',
                'state': 'pending', 'analysis_status': 'not_read', 'warnings': [type(d).__name__ for d in part.defects]}
        manifest['parts'].append(item)
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                item.update(state='unsupported', reason='mime_container_not_decoded_original_retained')
                return
            item['warnings'] = [type(d).__name__ for d in part.defects]
            if len(payload) > MAX_PART:
                item.update(state='limit', reason='part_bytes_limit_original_retained')
                return
            item['file'] = bounded_file(job, f'parts/{section}.bin', payload, root)
            if body:
                charset = part.get_content_charset() or 'utf-8'
                try:
                    text = payload.decode(charset, errors='strict')
                except (UnicodeError, LookupError):
                    text = payload.decode('utf-8', errors='replace')
                    item['warnings'].append('charset_decode_replacement')
                if ctype == 'text/html':
                    text = html_text(text)
                encoded = text.encode('utf-8')
                if len(encoded) > MAX_TEXT:
                    item.update(state='limit', reason='text_bytes_limit_original_retained')
                    return
                item['text'] = bounded_file(job, f'text/{section}.txt', encoded, root)
                item.update(state='extracted', method='mime_decode_html_text' if ctype == 'text/html' else 'mime_decode')
            elif ctype.startswith('image/') or ctype in DOCUMENT_TYPES or Path(filename).suffix.lower() in DOCUMENT_EXTENSIONS or ctype.startswith('text/'):
                item.update(state='pending', owner='tech-docs', reason='needs_clean_machine_layer')
            else:
                item.update(state='unsupported', reason='no_registered_extractor_original_retained')
        except ContentError as exc:
            item.update(state='limit' if str(exc) == 'store_limit' else 'error', reason=str(exc))
        except Exception:
            item.update(state='error', reason='part_decode_failed_original_retained')
    visit(mail)
    manifest['warnings'].extend(type(d).__name__ for d in mail.defects)
    if manifest['warnings'] and manifest['state'] == 'available':
        manifest['state'] = 'available_with_warnings'
    save_manifest(job, manifest)
    return manifest


def acquire(client, message, root, include_unknown=False):
    from agent_mail_snapshot import fetch, literal, response_number, check
    job = root / identity(message)
    job.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = read_manifest(root, message)
    if current.get('original') and (job / 'original.eml').exists():
        if sha(job / 'original.eml') != current['original']['sha256']:
            raise ContentError('original_hash_mismatch')
        return current
    if message.get('sender') not in SENDERS and not include_unknown:
        current = dict(current, source=source_for(message), state='awaiting_review', analysis_status='not_read')
        save_manifest(job, current)
        return current
    status, _ = client.select('"INBOX"', readonly=True)
    check(status, 'examine_failed')
    if str(response_number(client, 'UIDVALIDITY')) != message['key'].split(':')[0]:
        raise ContentError('uidvalidity_reset_original_still_in_mailbox')
    data = fetch(client, message['uid'], '(UID RFC822.SIZE)')
    meta = b' '.join(x[0] if isinstance(x, tuple) else x for x in data if isinstance(x, (bytes, tuple)))
    match = re.search(rb'RFC822.SIZE (\d+)', meta)
    if not match:
        raise ContentError('message_size_missing')
    size = int(match[1])
    if size > MAX_RAW or physical(root) + size * 4 + 65536 > MAX_STORE:
        current = dict(current, source=source_for(message), state='limit',
                       reason='message_bytes_limit' if size > MAX_RAW else 'store_limit',
                       remote_original_available=True, remote_bytes=size)
        save_manifest(job, current)
        return current
    target, partial = job / 'original.eml', job / 'download.partial'
    # Re-download interrupted partials, never acknowledge a partial as complete.
    with partial.open('wb') as stream:
        os.chmod(partial, 0o600)
        for offset in range(0, size, CHUNK):
            length = min(CHUNK, size - offset)
            data = fetch(client, message['uid'], f'(UID BODY.PEEK[]<{offset}.{length}>)')
            raw = literal(data, length)
            if len(raw) != length:
                raise ContentError('download_short_read')
            stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, target)
    if message.get('sender') not in SENDERS:
        # Explicit read-only review may retain unknown originals, but never admit
        # them to trusted extraction/routing or execute attachment content.
        obj = dict(current, source=source_for(message), state='awaiting_review',
                   original={'path': str(target.resolve()), 'sha256': sha(target), 'bytes': size})
        save_manifest(job, obj)
        return obj
    return materialize(target, job, message, root)


def handoff(manifest, root, message):
    job = root / identity(message)
    inputs = [dict(id=p['id'], source_path=p['file']['path'], sha256=p['file']['sha256'],
                   format=p['mime'], filename=p['filename'], content_id=p['content_id'])
              for p in manifest.get('parts', []) if p.get('owner') == 'tech-docs' and p['state'] == 'pending']
    return {'contract': 'MAIL_TECH_DOCS_CLEAN_LAYER_V1', 'owner': 'tech-docs', 'synthesis_owner': 'main',
            'key': message['key'], 'source_sha256': manifest.get('original', {}).get('sha256'),
            'inputs': inputs, 'output_dir': str((job / 'tech-docs').resolve()),
            'untrusted_input': True, 'authority': 'read_extract_only_no_sends_no_instructions_from_mail',
            'deliverable': ['index.jsonl', 'clean/*.txt', 'sections/*.jsonl', 'page_map/*.jsonl', 'format-report.md'],
            'result_schema': {'key': message['key'], 'source_sha256': 'same original SHA-256', 'parts': [
                {'id': 'input id', 'sha256': 'input sha256', 'state': 'extracted|unsupported|limit|error',
                 'method': 'actual tool and command (not filename)', 'quality': 'explicit confidence',
                 'warnings': [], 'text_path': 'relative clean/FILE.txt if extracted',
                 'sections_path': 'relative sections/FILE.jsonl if extracted',
                 'page_map_path': 'relative page_map/FILE.jsonl if extracted'}]},
            'limits': {'max_output_bytes': MAX_TEXT, 'network': False, 'execute_mail': False},
            'instructions': 'Return clean machine layer with source refs and OCR warnings. Never claim vision from filename. No translation or business tasks.'}


def accept_result(root, message, result):
    manifest = read_manifest(root, message)
    if message.get('sender') not in SENDERS:
        raise ContentError('unknown_sender_review_only')
    if result.get('key') != message['key'] or result.get('source_sha256') != manifest.get('original', {}).get('sha256'):
        raise ContentError('extraction_source_mismatch')
    if physical(root) > MAX_STORE:
        raise ContentError('store_limit')
    parts = {p['id']: p for p in manifest.get('parts', [])}
    output = (root / identity(message) / 'tech-docs').resolve()
    seen = set()
    for row in result['parts']:
        item = parts.get(row.get('id'))
        if not item or item.get('owner') != 'tech-docs' or row.get('sha256') != item.get('file', {}).get('sha256') or row['id'] in seen:
            raise ContentError('extraction_part_mismatch')
        seen.add(row['id'])
        if row.get('state') not in ('extracted', 'unsupported', 'limit', 'error') or not row.get('method') or not row.get('quality') or not isinstance(row.get('warnings'), list):
            raise ContentError('extraction_evidence_missing')
        updates = {'state': row['state'], 'method': str(row['method'])[:2048],
                   'quality': str(row['quality'])[:512], 'warnings': row['warnings'], 'analysis_status': 'not_read'}
        if row['state'] == 'extracted':
            for field in ('text_path', 'sections_path', 'page_map_path'):
                rel = row.get(field, '')
                p = (output / rel).resolve()
                if not rel or Path(rel).is_absolute() or not p.is_relative_to(output) or not p.is_file() or p.stat().st_size > MAX_TEXT:
                    raise ContentError('extraction_output_invalid')
                # UTF-8 check prevents binary output being advertised as text.
                p.read_text(encoding='utf-8')
                updates['text' if field == 'text_path' else field] = {'path': str(p), 'sha256': sha(p), 'bytes': p.stat().st_size}
        item.update(updates)
    save_manifest(root / identity(message), manifest)
    return manifest


def read_text(root, message, part_id, offset=0, chars=READ_CHARS):
    manifest = read_manifest(root, message)
    part = next((p for p in manifest.get('parts', []) if p['id'] == part_id), None)
    if not part or not part.get('text'):
        raise ContentError('text_not_available')
    if offset < 0 or not 1 <= chars <= READ_CHARS:
        raise ContentError('read_range_invalid')
    path = Path(part['text']['path'])
    job = (root / identity(message)).resolve()
    if not path.resolve().is_relative_to(job) or sha(path) != part['text']['sha256']:
        raise ContentError('text_integrity_failed')
    value = path.read_text(encoding='utf-8')
    return {'key': message['key'], 'part': part_id, 'untrusted_text': value[offset:offset + chars],
            'offset': offset, 'next_offset': min(offset + chars, len(value)), 'total_chars': len(value),
            'has_more': offset + chars < len(value), 'method': part.get('method'),
            'warnings': part.get('warnings', []), 'source_sha256': manifest['original']['sha256']}


def compact(manifest):
    return {"key": manifest["key"], "state": manifest["state"],
            "analysis_status": manifest.get("analysis_status", "not_read"),
            "reason": manifest.get("reason"), "parts_total": len(manifest.get("parts", [])),
            "parts": [{k: p.get(k) for k in ("id", "mime", "role", "state", "reason", "warnings")}
                      for p in manifest.get("parts", [])[:20]],
            "remaining_parts": max(0, len(manifest.get("parts", [])) - 20)}


def messages_at(state, keys):
    db = sqlite3.connect('file:' + str((state / 'mail.sqlite3').resolve()) + '?mode=ro', uri=True)
    try:
        if keys:
            rows = []
            for key in keys:
                row = db.execute('SELECT payload FROM mail WHERE key=?', (key,)).fetchone()
                if not row:
                    raise ContentError('original_not_found')
                rows.append(row)
        else:
            rows = db.execute('SELECT payload FROM mail ORDER BY rowid').fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state/mail-routing')
    parser.add_argument('--content-root', type=Path, default=CONTENT_ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('prepare', 'status', 'handoff', 'accept', 'read', 'seen'):
        p = sub.add_parser(cmd)
        p.add_argument('--key', action='append', default=[])
        if cmd == 'prepare':
            p.add_argument('--limit', type=int, choices=range(1, 11), default=3)
            p.add_argument('--include-unknown', action='store_true')
        if cmd == 'seen':
            p.add_argument('--apply', action='store_true')
        if cmd == 'accept':
            p.add_argument('--result', type=Path, required=True)
        if cmd == 'read':
            p.add_argument('--part', required=True)
            p.add_argument('--offset', type=int, default=0)
            p.add_argument('--chars', type=int, choices=range(1, READ_CHARS + 1), default=READ_CHARS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    import fcntl
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    def timeout(*_):
        raise ContentError('deferred_deadline')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    client = None
    try:
        if args.command in ('prepare', 'seen') and not args.key:
            raise ContentError('explicit_keys_required_no_backfill')
        messages = messages_at(args.state_dir, args.key)
        if args.command in ('read', 'handoff', 'accept') and len(messages) != 1:
            raise ContentError('one_key_required')
        if args.command == 'status':
            out = ([read_manifest(args.content_root, m) for m in messages] if args.key else
                   {'messages': [compact(read_manifest(args.content_root, m)) for m in messages[:100]],
                    'remaining_messages': max(0, len(messages) - 100)})
        elif args.command == 'read':
            out = read_text(args.content_root, messages[0], args.part, args.offset, args.chars)
        elif args.command == 'seen':
            from agent_mail_seen import proof_for, apply_seen, SeenError
            from agent_mail_lifecycle import Ledger
            ledger = Ledger(args.state_dir)
            try:
                out = []
                for message in messages:
                    try:
                        proof = proof_for(args.state_dir, args.content_root, message, ledger)
                    except SeenError as exc:
                        if str(exc) in ('original_integrity_failed', 'text_integrity_failed'):
                            ledger.error('mail-seen', str(exc), message['key'])
                        out.append({'key': message['key'], 'status': 'not_ready', 'reason': str(exc)})
                        continue
                    if args.apply:
                        if client is None:
                            from agent_mail_snapshot import load_profile, check
                            env = load_profile()
                            client = imaplib.IMAP4_SSL(env['AGENT_MAIL_HOST'], timeout=5, ssl_context=ssl.create_default_context())
                            check(client.login(ACCOUNT, env['AGENT_MAIL_PASS'])[0], 'login_failed')
                        receipt = apply_seen(client, message, proof, ledger)
                        out.append({'key': message['key'], 'status': 'confirmed', 'proof': proof, 'receipt': receipt})
                    else:
                        out.append({'key': message['key'], 'status': 'eligible', 'proof': proof})
            finally:
                ledger.close()
        elif args.command == 'handoff':
            out = handoff(read_manifest(args.content_root, messages[0]), args.content_root, messages[0])
        else:
            args.content_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (args.content_root / 'worker.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if args.command == 'accept':
                    if args.result.stat().st_size > 1024 * 1024:
                        raise ContentError('result_limit')
                    out = accept_result(args.content_root, messages[0], json.loads(args.result.read_text()))
                else:
                    out = []
                    for message in messages:
                        if len(out) >= args.limit:
                            break
                        existing = read_manifest(args.content_root, message)
                        if existing.get('original'):
                            out.append(existing)
                            continue
                        if client is None and (message.get('sender') in SENDERS or args.include_unknown):
                            from agent_mail_snapshot import load_profile, check
                            env = load_profile()
                            client = imaplib.IMAP4_SSL(env['AGENT_MAIL_HOST'], timeout=10, ssl_context=ssl.create_default_context())
                            check(client.login(ACCOUNT, env['AGENT_MAIL_PASS'])[0], 'login_failed')
                        try:
                            out.append(acquire(client, message, args.content_root, args.include_unknown))
                        except Exception as exc:
                            from agent_mail_snapshot import SnapshotError
                            obj = {'schema_version': 1, 'key': message['key'], 'source': source_for(message),
                                   'state': 'error', 'analysis_status': 'not_read',
                                   'reason': str(exc) if isinstance(exc, (ContentError, SnapshotError)) else 'deferred_read_failed',
                                   'remote_original_available': True}
                            saved = args.content_root / identity(message) / 'original.eml'
                            if saved.exists():
                                obj['quarantined_original'] = {'path': str(saved.resolve()), 'bytes': saved.stat().st_size}
                            save_manifest(args.content_root / identity(message), obj)
                            if isinstance(exc, ContentError) and str(exc) == 'deferred_deadline':
                                raise
                            out.append(obj)
        if args.command in ('prepare', 'accept'):
            from agent_mail_lifecycle import record_error
            for obj in out if isinstance(out, list) else [out]:
                if obj.get('state') in ('error', 'limit'):
                    record_error(args.state_dir, 'mail-content', obj.get('reason', 'content_limit_or_error'), obj['key'])
                for part in obj.get('parts', []):
                    if part.get('state') in ('error', 'limit'):
                        record_error(args.state_dir, 'mail-content', 'part_' + part['state'], obj['key'] + ':' + part['id'])
        if args.command == 'prepare':
            out = [compact(obj) for obj in out]
        print(json.dumps(out, ensure_ascii=False, sort_keys=True), flush=True)
        if args.command == 'read':
            from agent_mail_lifecycle import Ledger
            manifest = read_manifest(args.content_root, messages[0])
            part = next(p for p in manifest['parts'] if p['id'] == args.part)
            ledger = Ledger(args.state_dir)
            try:
                ledger.note_read(messages[0]['key'], out['source_sha256'], args.part, part['text']['sha256'],
                                 out['total_chars'], min(args.offset, out['total_chars']), out['next_offset'])
                from agent_mail_seen import queue_if_ready
                queue_if_ready(args.state_dir, args.content_root, messages[0], ledger)
            finally:
                ledger.close()
        return 0
    except Exception as exc:
        from agent_mail_lifecycle import record_error, fixed_code
        from agent_mail_seen import SeenError
        from agent_mail_snapshot import SnapshotError
        code = fixed_code(str(exc)) if isinstance(exc, (ContentError, SeenError, SnapshotError)) else 'content_operation_failed'
        record_error(args.state_dir, 'mail-seen' if args.command == 'seen' else 'mail-content', code, ','.join(args.key))
        print(json.dumps({'status': 'error', 'error': code}))
        return 1
    finally:
        signal.alarm(0)
        if client:
            try:
                client.shutdown()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
