"""Host-owned decision admission in existing mail.meta; not wired to runtime.

The caller owns the SQLite connection, catalog verification and model invocation.
No table creation, mail access, model calls or delivery is performed here.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import sqlite3

from agent_mail_routing import Catalog, Decision, SelectionInput, Shortlist, cache_key, decide, model_input

_PREFIX = 'semantic_decision_v1:'
_GATE_PREFIX = 'semantic_message_v1:'


@dataclass(frozen=True)
class Reservation:
    status: str  # admitted, held, completed
    key: str
    token: str | None = None
    decision: Decision | None = None


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _saved_decision(value, source, options):
    if (not isinstance(value, dict) or set(value) != {'status', 'candidate_id', 'reason', 'intake_reason', 'source_report_only'}
            or value['intake_reason'] != source.intake_reason
            or value['source_report_only'] is not source.source_report_only):
        return None
    if value['status'] == 'selected':
        if (value['reason'] != 'matched' or not isinstance(value['candidate_id'], str)
                or value['candidate_id'] not in {c.candidate_id for c in options.entries}):
            return None
    elif value['status'] == 'held':
        if value['candidate_id'] is not None or value['reason'] not in {
                'invalid_output', 'ambiguous', 'abstained', 'insufficient_evidence'}:
            return None
    else:
        return None
    return Decision(value['status'], value['candidate_id'], value['reason'],
                    value['intake_reason'], value['source_report_only'])


def _message_gate(cat, source):
    identity = _encode([cat.scope.owner, cat.scope.account, source.mail_identity]).encode('utf-8')
    return _GATE_PREFIX + hashlib.sha256(identity).hexdigest()


def _read_record(row):
    try:
        return json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def reserve(db: sqlite3.Connection, cat: Catalog, source: SelectionInput, options: Shortlist) -> Reservation:
    """Persist one classifier admission before a call, with no transaction across it.

    A pre-existing reservation is held indefinitely rather than inferred safe to
    retry after a crash. No absence of a remote receipt proves non-execution.
    """
    model_input(source, options)  # Reject incomplete, empty or ineligible requests.
    key = cache_key(cat, source, options)
    db.execute('BEGIN IMMEDIATE')
    try:
        gate_key = _message_gate(cat, source)
        gate_row = db.execute('SELECT value FROM meta WHERE key=?', (gate_key,)).fetchone()
        gate = _read_record(gate_row)
        row = db.execute('SELECT value FROM meta WHERE key=?', (_PREFIX + key,)).fetchone()
        valid_gate = (isinstance(gate, dict) and type(gate.get('version')) is int
                      and gate.get('version') == 1 and gate.get('status') in ('reserved', 'complete')
                      and isinstance(gate.get('key'), str) and re.fullmatch(r'[a-f0-9]{64}', gate['key']))
        if valid_gate and gate['status'] == 'complete':
            previous = _read_record(db.execute('SELECT value FROM meta WHERE key=?', (_PREFIX + gate['key'],)).fetchone())
            valid_gate = (set(gate) == {'version', 'status', 'key'} and isinstance(previous, dict)
                          and previous.get('version') == 1 and previous.get('status') == 'complete')
        if gate_row and not valid_gate:
            result = Reservation('held', key)
        elif gate and gate['status'] == 'reserved':
            # A crash or unknown model outcome remains held even if revisions change.
            result = Reservation('held', key)
        elif row is None:
            token = secrets.token_hex(32)
            db.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                       (_PREFIX + key, _encode({'version': 1, 'status': 'reserved', 'token': token})))
            db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (gate_key, _encode({'version': 1, 'status': 'reserved', 'key': key, 'token': token})))
            result = Reservation('admitted', key, token)
        else:
            record = _read_record(row)
            decision = (_saved_decision(record.get('decision'), source, options)
                        if isinstance(record, dict) and record.get('version') == 1
                        and record.get('status') == 'complete' else None)
            result = Reservation('completed', key, decision=decision) if decision else Reservation('held', key)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def finalize(db: sqlite3.Connection, cat: Catalog, source: SelectionInput, options: Shortlist,
             token: str, raw_output: str) -> Decision:
    """Validate the response and durably settle only its original reservation."""
    model_input(source, options)
    key = cache_key(cat, source, options)
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError('reservation_invalid')
    decision = decide(source, options, raw_output)
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT value FROM meta WHERE key=?', (_PREFIX + key,)).fetchone()
        record = _read_record(row)
        gate_key = _message_gate(cat, source)
        gate = _read_record(db.execute('SELECT value FROM meta WHERE key=?', (gate_key,)).fetchone())
        if (not isinstance(record, dict) or set(record) != {'version', 'status', 'token'}
                or record['version'] != 1 or record['status'] != 'reserved'
                or not isinstance(record['token'], str) or not record['token'].isascii()
                or not token.isascii() or not secrets.compare_digest(record['token'], token)
                or not isinstance(gate, dict) or gate != {'version': 1, 'status': 'reserved', 'key': key, 'token': token}):
            raise ValueError('reservation_invalid')
        saved = {'status': decision.status, 'candidate_id': decision.candidate_id,
                 'reason': decision.reason, 'intake_reason': decision.intake_reason,
                 'source_report_only': decision.source_report_only}
        db.execute('UPDATE meta SET value=? WHERE key=?',
                   (_encode({'version': 1, 'status': 'complete', 'decision': saved}), _PREFIX + key))
        db.execute('UPDATE meta SET value=? WHERE key=?',
                   (_encode({'version': 1, 'status': 'complete', 'key': key}), gate_key))
        db.commit()
        return decision
    except BaseException:
        db.rollback()
        raise
