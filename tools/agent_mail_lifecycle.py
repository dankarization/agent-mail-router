#!/usr/bin/env python3
"""Mail-only durable read evidence, exact-UID Seen intents, and error outbox."""
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agent_mail_settings import MAIL_TOPIC
COMPONENTS = ('mail-monitor', 'mail-content', 'mail-delivery', 'mail-service', 'mail-seen')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fixed_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value) else 'mail_operation_failed'


class Ledger:
    def __init__(self, state):
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / 'lifecycle.sqlite3', timeout=1)
        os.chmod(state / 'lifecycle.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS lifecycle_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS errors (
                id TEXT PRIMARY KEY, component TEXT NOT NULL, code TEXT NOT NULL,
                scope TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
                occurrences INTEGER NOT NULL, status TEXT NOT NULL,
                receipt TEXT, delivery_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen_attempts (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL,
                receipt TEXT, error TEXT, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reads (
                key TEXT NOT NULL, source TEXT NOT NULL, part TEXT NOT NULL,
                text_sha TEXT NOT NULL, total INTEGER NOT NULL, ranges TEXT NOT NULL,
                PRIMARY KEY(key, source, part, text_sha));
            CREATE TABLE IF NOT EXISTS seen (
                key TEXT PRIMARY KEY, proof TEXT NOT NULL, status TEXT NOT NULL,
                receipt TEXT, error TEXT);
        ''')
        fallback = state / 'errors-fallback.jsonl'
        if fallback.exists():
            # Keep the original append-only journal; transactionally track bytes
            # replayed so each event is imported once after SQLite recovers.
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO lifecycle_meta VALUES (?,0)', ('fallback_offset',))
                offset = self.db.execute('SELECT value FROM lifecycle_meta WHERE key=?', ('fallback_offset',)).fetchone()[0]
                with fallback.open('rb') as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b'\n'):
                            break
                        try:
                            event = json.loads(line)
                            component = event.get('component') if event.get('component') in COMPONENTS else 'mail-monitor'
                            self._error(component, fixed_code(event.get('code')), '', event.get('time', time.time()))
                        except (ValueError, TypeError):
                            self._error('mail-monitor', 'fallback_journal_invalid', '', time.time())
                        offset += len(line)
                self.db.execute('UPDATE lifecycle_meta SET value=? WHERE key=?', (offset, 'fallback_offset'))

    def close(self):
        self.db.close()

    def error(self, component, code, scope='', now=None):
        if component not in COMPONENTS:
            raise ValueError('mail_component_invalid')
        code = fixed_code(code)
        scope = hashlib.sha256(str(scope).encode()).hexdigest()[:24] if scope else ''
        now = time.time() if now is None else now
        with self.db:
            return self._error(component, code, scope, now)

    def _error(self, component, code, scope, now):
        eid = hashlib.sha256(encoded([component, code, scope]).encode()).hexdigest()[:32]
        self.db.execute('''INSERT INTO errors
                (id,component,code,scope,first,last,occurrences,status)
                VALUES (?,?,?,?,?,?,1,'pending') ON CONFLICT(id) DO UPDATE SET
                last=excluded.last,occurrences=errors.occurrences+1''',
            (eid, component, code, scope, now, now))
        return eid

    def note_read(self, key, source, part, text_sha, total, start, end):
        if not 0 <= start <= end <= total:
            raise ValueError('read_evidence_invalid')
        with self.db:
            row = self.db.execute('SELECT ranges FROM reads WHERE key=? AND source=? AND part=? AND text_sha=?',
                                  (key, source, part, text_sha)).fetchone()
            ranges = sorted((json.loads(row[0]) if row else []) + [[start, end]])
            merged = []
            for lo, hi in ranges:
                if merged and lo <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            self.db.execute('INSERT OR REPLACE INTO reads VALUES (?,?,?,?,?,?)',
                            (key, source, part, text_sha, total, encoded(merged)))

    def fully_read(self, key, source, part, text_sha, total):
        row = self.db.execute('SELECT ranges,total FROM reads WHERE key=? AND source=? AND part=? AND text_sha=?',
                              (key, source, part, text_sha)).fetchone()
        return bool(row and row['total'] == total and json.loads(row['ranges']) == [[0, total]])

    def intent(self, key, proof):
        with self.db:
            old = self.db.execute('SELECT proof FROM seen WHERE key=?', (key,)).fetchone()
            if old and old[0] != encoded(proof):
                raise ValueError('seen_proof_changed')
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,'pending',NULL,NULL)", (key, encoded(proof)))

    def seen_result(self, key, status, receipt=None, error=None):
        with self.db:
            self.db.execute('INSERT INTO seen_attempts(key,status,receipt,error,at) VALUES (?,?,?,?,?)',
                            (key, status, encoded(receipt) if receipt else None, error, time.time()))
            self.db.execute('UPDATE seen SET status=?,receipt=?,error=? WHERE key=?',
                            (status, encoded(receipt) if receipt else None, error, key))

    def dispatch_error(self, telegram, clock=None):
        now = clock() if clock else datetime.now(timezone.utc)
        if not 10 <= now.astimezone(ZoneInfo('Asia/Tbilisi')).hour < 24:
            return {'status': 'quiet'}
        row = self.db.execute("SELECT * FROM errors WHERE status='pending' AND next_attempt<=? ORDER BY first,id LIMIT 1",
                              (now.timestamp(),)).fetchone()
        if not row:
            return {'status': 'quiet'}
        text = ('<b>Ошибка почтового монитора</b>\n' + html.escape(row['component'] + ': ' + row['code']) +
                '\nПовторений: ' + str(row['occurrences']) +
                '\nОшибка сохранена; данные и cursor не сбрасываются.\nMAIL-ERROR-' + row['id'])
        # Atomic claim excludes simultaneous router/worker dispatchers.
        with self.db:
            changed = self.db.execute("UPDATE errors SET status='inflight',attempts=attempts+1 WHERE id=? AND status='pending'",
                                      (row['id'],)).rowcount
        if not changed:
            return {'status': 'busy'}
        try:
            result = telegram.send(MAIL_TOPIC, text)
        except Exception:
            result = {'status': 'unknown', 'error': 'telegram_ack_unknown'}
        status = result.get('status')
        if status == 'deferred':
            status = 'pending'
        if status not in ('sent', 'pending', 'unknown', 'rejected') or (status == 'sent' and not result.get('receipt')):
            status = 'unknown'
        with self.db:
            self.db.execute('UPDATE errors SET status=?,receipt=?,delivery_error=?,next_attempt=? WHERE id=?',
                            (status, result.get('receipt'), fixed_code(result['error']) if result.get('error') else None,
                             now.timestamp() + min(600, 30 * 2 ** min(row['attempts'], 4)) if status == 'pending' else 0, row['id']))
        return {'status': status, 'id': row['id']}

    def status(self):
        return {'errors': [dict(r) for r in self.db.execute('SELECT * FROM errors ORDER BY first LIMIT 100')],
                'error_counts': [dict(r) for r in self.db.execute('SELECT status,count(*) AS count FROM errors GROUP BY status')],
                'seen': [dict(r) for r in self.db.execute('SELECT key,status,receipt,error FROM seen ORDER BY rowid LIMIT 100')]}


def record_error(state, component, code, scope=''):
    try:
        ledger = Ledger(state)
        try:
            return ledger.error(component, code, scope)
        finally:
            ledger.close()
    except Exception:
        # Independent last-resort evidence if SQLite cannot accept the error.
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(state / 'errors-fallback.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (encoded({'component': component, 'code': fixed_code(code), 'time': time.time()}) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(state, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return 'fallback_journal'
