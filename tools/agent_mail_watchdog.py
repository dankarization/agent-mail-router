#!/usr/bin/env python3
"""Mail-only observations, activated analysis and bounded repair."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

from agent_mail_lifecycle import Ledger, COMPONENTS, encoded

TZ = ZoneInfo('Asia/Tbilisi')
LANES = ('scheduler', 'observer') + COMPONENTS
STABLE_SECONDS = 300
RECOVERY_SECONDS = 900
MAX_ROWS = 2048
CAP = {'total_tokens': 8000, 'daily_tokens': 16000, 'daily_attempts': 2,
       'read_only_tools': 4, 'seconds': 60}
# Bounded repair is a separate, explicitly activated allowlist. It never
# diagnoses, edits code, restarts services, replays an unknown outcome or
# reclaims an analysis admission. At most one action per tick, capped attempts
# with backoff, a durable lease against concurrent repair, and success only
# from independently observed health on a later ordinary observation.
REPAIR_ACTIONS = {
    ('observer', 'error_journal_unreplayed'): {'action': 'replay_error_journal',
                                               'attempts': 3, 'backoff': (60, 300, 900)},
    ('mail-delivery', 'mail-delivery_overdue'): {'action': 'dispatch_pending_effect',
                                                 'attempts': 2, 'backoff': (300, 900)},
    ('mail-seen', 'mail-seen_held'): {'action': 'apply_seen_once',
                                      'attempts': 2, 'backoff': (300, 900)},
}
REPAIR_LEASE = 120
REPAIR_ACTION_TIMEOUT = 45
REPAIR_ACTIONS_PER_TICK = 1
LOCAL_REPAIRS = ('replay_error_journal',)
REPAIR_TICK_BUDGET = 12


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()[:32]


def readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 2
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return db


def limited(db, query, args=()):
    rows = db.execute(query, args).fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise ValueError('observation_limit')
    return [dict(r) for r in rows]


def sibling(name):
    """Sibling helper path that survives the deployed import-prefix rename."""
    current = Path(__file__).resolve()
    return current.parent / current.name.replace('watchdog.py', name)


def sibling_stem(filename, suffix):
    """Module stem for a sibling helper, canonical or deployed prefix."""
    if not filename.endswith('watchdog.py'):
        raise ValueError('repair_sibling_name')
    return filename[:-len('watchdog.py')] + suffix


def sibling_module(suffix):
    """Import a sibling under this file's prefix, not a hard-coded package name."""
    import importlib
    return importlib.import_module(sibling_stem(Path(__file__).name, suffix))


def delegate_argv(action, state, key=None):
    """Fixed argv for the two idempotent delegate actions; no stored argv.

    The executor helper owns process launch so this observer keeps its
    no-subprocess invariant.
    """
    if action == 'dispatch_pending_effect':
        return [sys.executable, str(sibling('router.py')), '--state-dir', str(state), 'dispatch']
    if action == 'apply_seen_once':
        return [sys.executable, str(sibling('content.py')), '--state-dir', str(state),
                'seen', '--key', key, '--apply']
    raise ValueError('repair_action_unsupported')


def scheduler(db, job, store, now):
    # Never select payload, raw errors, summaries, targets or credentials.
    fields = ('lastRunAtMs', 'lastRunStatus', 'lastStatus', 'lastTriggerEvalAtMs',
              'consecutiveErrors', 'scheduleErrorCount', 'runningAtMs', 'nextRunAtMs')
    safe_state = "CASE WHEN json_valid(state_json) THEN CASE WHEN json_type(state_json)='object' THEN state_json ELSE '{}' END ELSE '{}' END"
    project = ','.join("json_extract(" + safe_state + ",'$." + f + "') AS " + f for f in fields)
    rows = limited(db, 'SELECT enabled,' + project + ",json_extract(CASE WHEN json_valid(job_json) THEN job_json ELSE '{}' END,'$.schedule.everyMs') AS everyMs "
                   'FROM cron_jobs WHERE job_id=? AND store_key=? '
                   "AND json_valid(state_json) AND json_type(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END)='object' "
                   "AND json_valid(job_json) AND json_type(CASE WHEN json_valid(job_json) THEN job_json ELSE '{}' END)='object'", (job, store))
    if len(rows) != 1:
        raise ValueError('scheduler_missing_or_schema')
    row = rows[0]
    run = int(row['lastRunAtMs'] or 0) / 1000
    evaluation = int(row['lastTriggerEvalAtMs'] or 0) / 1000
    errors = int(row['consecutiveErrors'] or 0)
    interval = int(row['everyMs'] or 900000) / 1000
    success = max(run if (row['lastRunStatus'] or row['lastStatus']) == 'ok' else 0,
                  evaluation if errors == 0 else 0)
    failure = (row['lastRunStatus'] or row['lastStatus']) == 'error' and (run >= success or errors > 0)
    code = ('scheduler_disabled' if not row['enabled'] else
            'scheduler_schedule_failed' if row['scheduleErrorCount'] else
            'scheduler_execution_failed' if failure else
            'scheduler_overdue' if now - max(run, evaluation) > max(3600, interval * 3) else '')
    if interval <= 0:
        raise ValueError('scheduler_interval_invalid')
    return {'code': code, 'event': digest([run, evaluation, errors, code]),
            'success': success, 'interval': interval}


def collect(state, scheduler_path, job, store, now):
    """Partial observations cannot resolve incidents. All source handles are RO."""
    out = {'valid': True, 'errors': [], 'lanes': {}, 'success': 0}
    try:
        with closing(readonly(scheduler_path)) as db:
            row = scheduler(db, job, store, now)
            out['lanes']['scheduler'] = {'code': row['code'], 'event': row['event']}
            out['success'] = row['success']
            out['service_interval'] = row['interval']
        with closing(readonly(state / 'lifecycle.sqlite3')) as db:
            out['errors'] = limited(db, "SELECT id,component,occurrences,last,status FROM errors WHERE code!='watchdog_unresolved' ORDER BY id")
            if any(r['component'] not in COMPONENTS or not isinstance(r['id'], str)
                   or len(r['id']) != 32 or any(c not in '0123456789abcdef' for c in r['id'])
                   or not isinstance(r['occurrences'], int) or r['occurrences'] < 1 for r in out['errors']):
                out['errors'] = []
                raise ValueError('error_metadata_invalid')
            seen = limited(db, "SELECT key,status FROM seen WHERE status IN ('pending','unknown','inflight','error') ORDER BY key")
        with closing(readonly(state / 'mail.sqlite3')) as db:
            effects = limited(db, "SELECT id,summary_id,kind,status FROM effects WHERE status NOT IN ('sent','unavailable') ORDER BY id")
            batches = limited(db, 'SELECT id FROM batches WHERE result IS NULL ORDER BY id')
        daytime = datetime.fromtimestamp(now, TZ).hour >= 10
        contexts = {r['summary_id'] for r in effects if r['kind'] == 'context' and r['status'] == 'pending'}
        # Pending is work, not a failed send. Age notify only while transport is
        # eligible and its own contexts have been serviced (held ACKs stay held).
        out['delivery_pending'] = [{'id': r['id'], 'kind': r['kind']} for r in effects
                                   if r['status'] == 'pending' and
                                   (r['kind'] == 'context' or (daytime and r['summary_id'] not in contexts))]
        effects = [{k: r[k] for k in ('id', 'kind', 'status')} for r in effects if r['status'] != 'pending']
        for lane, rows in (('mail-delivery', effects), ('mail-seen', seen)):
            out['lanes'][lane] = {'code': lane + '_held' if rows else '', 'event': digest(rows)}
        out['lanes']['mail-monitor'] = {'code': 'summary_pending' if batches else '', 'event': digest(batches)}
        journal = state / 'errors-fallback.jsonl'
        if journal.exists() and journal.stat().st_size:
            with closing(readonly(state / 'lifecycle.sqlite3')) as db:
                row = db.execute("SELECT value FROM lifecycle_meta WHERE key='fallback_offset'").fetchone()
            if not row or row[0] < journal.stat().st_size:
                out['lanes']['observer'] = {'code': 'error_journal_unreplayed', 'event': str(journal.stat().st_size)}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        out['valid'] = False
        out['lanes']['observer'] = {'code': 'mail_observation_unavailable', 'event': 'unavailable'}
    return out


class Watchdog:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS wd_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wd_errors(id TEXT PRIMARY KEY,count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS wd_incidents(
                id TEXT PRIMARY KEY,lane TEXT NOT NULL,generation INTEGER NOT NULL,
                opened REAL NOT NULL,last REAL NOT NULL,event TEXT NOT NULL,
                repeats INTEGER NOT NULL,healthy_since REAL,closed REAL,
                decision TEXT,day TEXT,reserved INTEGER NOT NULL DEFAULT 0,
                alert TEXT,origin TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS wd_open_lane ON wd_incidents(lane) WHERE closed IS NULL;
            CREATE TABLE IF NOT EXISTS wd_days(day TEXT PRIMARY KEY,decisions INTEGER NOT NULL,tokens INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS wd_policy_audit(incident TEXT PRIMARY KEY,at REAL NOT NULL,previous TEXT NOT NULL,policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wd_analysis(
                incident TEXT PRIMARY KEY,day TEXT NOT NULL,started REAL NOT NULL,
                completed REAL,status TEXT NOT NULL,result TEXT,metadata TEXT,
                main_status TEXT NOT NULL DEFAULT 'pending',main_receipt TEXT,
                notify_status TEXT NOT NULL DEFAULT 'pending',notify_receipt TEXT);
            CREATE TABLE IF NOT EXISTS wd_repairs(
                incident TEXT PRIMARY KEY,lane TEXT NOT NULL,code TEXT NOT NULL,
                action TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                first REAL NOT NULL,last REAL NOT NULL,
                next_attempt REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL,result_code TEXT,verified REAL);

        ''')
        # Additive durable lane classification; pre-existing rows stay NULL and
        # are reclassified by the next ordinary observation.
        if 'code' not in {row[1] for row in self.db.execute('PRAGMA table_info(wd_incidents)')}:
            self.db.execute('ALTER TABLE wd_incidents ADD COLUMN code TEXT')

    def close(self):
        self.db.close()

    def observe(self, snapshot, now):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute("INSERT INTO wd_meta VALUES ('analysis_snapshot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encoded(snapshot),))
            first = not self.db.execute("SELECT 1 FROM wd_meta WHERE key='baseline'").fetchone()
            signals = dict(snapshot['lanes'])
            pending = {}
            if snapshot['valid']:
                previous = self.db.execute("SELECT value FROM wd_meta WHERE key='delivery_pending_v1'").fetchone()
                previous = json.loads(previous[0]) if previous else {}
                interval = snapshot.get('service_interval', 900)
                overdue = []
                for item in snapshot.get('delivery_pending', []):
                    old = previous.get(item['id'], {})
                    # A gap or ineligible observation cannot prove service time.
                    since = old.get('since', now) if 0 <= now - old.get('last', now) <= interval else now
                    pending[item['id']] = {'since': since, 'last': now}
                    if now - since >= interval:
                        overdue.append(item)
                if overdue:
                    lane = signals.get('mail-delivery', {})
                    signals['mail-delivery'] = {'code': lane.get('code') or 'mail-delivery_overdue',
                                               'event': digest([lane.get('event'), overdue])}
            self.db.execute("INSERT INTO wd_meta VALUES ('delivery_pending_v1',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (encoded(pending),))
            for error in snapshot['errors']:
                old = self.db.execute('SELECT count FROM wd_errors WHERE id=?', (error['id'],)).fetchone()
                changed = not old or error['occurrences'] > old[0]
                # Our own notification rows must not recursively create incidents.
                ours = self.db.execute('SELECT 1 FROM wd_incidents WHERE alert=?', (error['id'],)).fetchone()
                # A stored counter may advance for an occurrence already covered
                # by the initial baseline, a previous observation, or the
                # observer's own blind gap (for example while the scheduler and
                # this watcher were down across a Gateway restart). Only a
                # genuinely recent occurrence may open a runtime_failure
                # incident; otherwise an old alarm is re-admitted as a new
                # generation and its stale text is re-delivered (#53971).
                last = error.get('last')
                recent = isinstance(last, (int, float)) and last >= now - STABLE_SECONDS
                if changed and recent and not first and not ours and error['component'] in COMPONENTS:
                    previous = signals.get(error['component'], {})
                    signals[error['component']] = {'code': 'runtime_failure',
                                                   'event': digest([previous.get('event'), error['id'], error['occurrences']]),
                                                   'origin': previous.get('origin', error['id'])}
                self.db.execute('INSERT INTO wd_errors VALUES (?,?) ON CONFLICT(id) DO UPDATE SET count=excluded.count',
                                (error['id'], error['occurrences']))
            if first and snapshot['valid']:
                self.db.execute("INSERT INTO wd_meta VALUES ('baseline',?)", (str(now),))
                # Ignore historical error events, but monitor any current structural
                # failure on the next check (only closed history is baselined).
            else:
                for lane in LANES:
                    row = self.db.execute('SELECT * FROM wd_incidents WHERE lane=? AND closed IS NULL', (lane,)).fetchone()
                    signal_row = signals.get(lane, {})
                    bad = bool(signal_row.get('code'))
                    if lane in COMPONENTS and row and row['origin'] and not bad:
                        # Poll success cannot prove failed content/extraction healed.
                        bad = lane == 'mail-content' or snapshot['success'] <= row['last']
                        signal_row = {'event': row['event'], 'origin': row['origin']}
                    if bad:
                        if not row:
                            generation = self.db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM wd_incidents WHERE lane=?', (lane,)).fetchone()[0]
                            iid = digest([lane, generation])
                            self.db.execute('INSERT INTO wd_incidents(id,lane,generation,opened,last,event,repeats,origin,code) VALUES (?,?,?,?,?,?,1,?,?)',
                                            (iid, lane, generation, now, now, signal_row['event'], signal_row.get('origin'), signal_row.get('code')))
                        else:
                            self.db.execute('UPDATE wd_incidents SET last=?,event=?,repeats=repeats+1,healthy_since=NULL,origin=COALESCE(origin,?),code=? WHERE id=?',
                                            (now if signal_row['event'] != row['event'] else row['last'], signal_row['event'], signal_row.get('origin'), signal_row.get('code'), row['id']))
                    elif row and not snapshot['valid']:
                        self.db.execute('UPDATE wd_incidents SET healthy_since=NULL WHERE id=?', (row['id'],))
                    elif row and snapshot['valid']:
                        since = row['healthy_since'] if row['healthy_since'] is not None else now
                        closed = now if now - since >= RECOVERY_SECONDS else None
                        self.db.execute('UPDATE wd_incidents SET healthy_since=?,closed=? WHERE id=?', (since, closed, row['id']))
                        if closed is not None:
                            # Independent health only; never the action's own start.
                            self.db.execute("UPDATE wd_repairs SET outcome='recovered',verified=? WHERE incident=? AND outcome='attempted'", (now, row['id']))
                for row in self.db.execute('SELECT * FROM wd_incidents WHERE closed IS NULL AND decision IS NULL AND healthy_since IS NULL AND opened<=?',
                                           (now - STABLE_SECONDS,)).fetchall():
                    day = datetime.fromtimestamp(now, TZ).date().isoformat()
                    self.db.execute('INSERT OR IGNORE INTO wd_days VALUES (?,0,0)', (day,))
                    budget = self.db.execute('SELECT * FROM wd_days WHERE day=?', (day,)).fetchone()
                    reserve = budget['decisions'] < CAP['daily_attempts'] and budget['tokens'] + CAP['total_tokens'] <= CAP['daily_tokens']
                    enabled = self.db.execute("SELECT 1 FROM wd_meta WHERE key='paid_analysis_v2'").fetchone()
                    decision = 'analysis_ready' if enabled else ('hard_cap_unavailable' if reserve else 'daily_budget_block')
                    if reserve:
                        self.db.execute('UPDATE wd_days SET decisions=decisions+1,tokens=tokens+? WHERE day=?', (CAP['total_tokens'], day))
                    # Terminal durable reservation before ANY consumer or delivery.
                    # The separately activated analyzer owns paid admission.
                    self.db.execute('UPDATE wd_incidents SET decision=?,day=?,reserved=? WHERE id=?',
                                    (decision, day, CAP['total_tokens'] if reserve else 0, row['id']))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.status()

    def enable_repair(self, actions, now):
        """Explicit audited activation of individual repair actions."""
        with self.db:
            for action in actions:
                if action not in {spec['action'] for spec in REPAIR_ACTIONS.values()}:
                    raise ValueError('repair_action_unknown')
                self.db.execute("INSERT OR IGNORE INTO wd_meta VALUES (?,?)", ('repair_action:' + action, 'v1:' + str(now)))

    def repair_actions(self):
        return {spec['action'] for spec in REPAIR_ACTIONS.values()
                if self.db.execute('SELECT 1 FROM wd_meta WHERE key=?', ('repair_action:' + spec['action'],)).fetchone()}

    def plan(self, action):
        """Read-only candidate discovery; never mutates another store."""
        state = Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent
        if action == 'replay_error_journal':
            journal = state / 'errors-fallback.jsonl'
            return {'kind': 'local', 'bytes': journal.stat().st_size if journal.exists() else 0}
        if action == 'dispatch_pending_effect':
            with closing(readonly(state / 'mail.sqlite3')) as db:
                held = db.execute("SELECT count(*) FROM effects WHERE status IN ('inflight','unknown','rejected')").fetchone()[0]
                pending = db.execute("SELECT count(*) FROM effects WHERE status='pending'").fetchone()[0]
            if held or not pending:
                return {'kind': 'delegate', 'argv': None, 'reason': 'no_safe_pending_candidate'}
            return {'kind': 'delegate', 'argv': delegate_argv(action, state)}
        if action == 'apply_seen_once':
            with closing(readonly(state / 'lifecycle.sqlite3')) as db:
                row = db.execute("SELECT key FROM seen WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return {'kind': 'delegate', 'argv': None, 'reason': 'no_pending_seen'}
            return {'kind': 'delegate', 'argv': delegate_argv(action, state, row[0]), 'key': row[0]}
        raise ValueError('repair_action_unsupported')

    def repair(self, now, runner=None, budget=REPAIR_TICK_BUDGET):
        """One bounded allowlisted repair for a currently open, unhealed incident.

        Attempts and a lease are durably claimed before any action; unknown or
        failed outcomes are never retried. Only a later ordinary healthy
        observation verifies a claimed success. Incident rows, cursors,
        receipts, admissions and summaries are never modified here. Process
        launch is owned by the separate executor helper.
        """
        active = self.repair_actions()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            claimed = None
            for row in self.db.execute("SELECT * FROM wd_incidents WHERE closed IS NULL AND healthy_since IS NULL AND code IS NOT NULL ORDER BY opened,id").fetchall():
                spec = REPAIR_ACTIONS.get((row['lane'], row['code']))
                if not spec or spec['action'] not in active:
                    continue
                prior = self.db.execute('SELECT * FROM wd_repairs WHERE incident=?', (row['id'],)).fetchone()
                attempts = prior['attempts'] if prior else 0
                if prior and (prior['outcome'] == 'unknown' or attempts >= spec['attempts']
                              or prior['next_attempt'] > now or prior['lease_until'] > now):
                    continue
                plan = self.plan(spec['action'])
                first = prior['first'] if prior else now
                delay = spec['backoff'][min(attempts, len(spec['backoff']) - 1)]
                self.db.execute('''INSERT INTO wd_repairs
                    (incident,lane,code,action,attempts,first,last,next_attempt,lease_until,outcome,result_code,verified)
                    VALUES (?,?,?,?,?,?,?,?,?,'attempted',NULL,NULL) ON CONFLICT(incident) DO UPDATE SET
                    code=excluded.code,action=excluded.action,attempts=excluded.attempts,last=excluded.last,
                    next_attempt=excluded.next_attempt,lease_until=excluded.lease_until,
                    outcome=excluded.outcome,result_code=NULL''',
                    (row['id'], row['lane'], row['code'], spec['action'], attempts + 1, first, now,
                     now + delay, now + REPAIR_LEASE))
                claimed = {'incident': row['id'], 'lane': row['lane'], 'action': spec['action'], 'plan': plan}
                break
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        if claimed is None:
            return {'status': 'idle'}
        return self._execute_repair(claimed, runner, budget)

    def _execute_repair(self, claimed, runner, budget):
        started = time.monotonic()
        action, plan = claimed['action'], claimed['plan']
        outcome, code = 'ok', None
        try:
            if action in LOCAL_REPAIRS:
                state = Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent
                with closing(Ledger(state)) as ledger:
                    ledger.close()
                code = 'repair_error_journal_replayed'
            elif plan.get('argv') is None:
                outcome, code = 'skipped', plan.get('reason')
            elif runner is None:
                # No executor attached: classify only, never act.
                outcome, code = 'skipped', 'repair_executor_required'
            else:
                remaining = int(budget - (time.monotonic() - started))
                if remaining < 2:
                    outcome, code = 'failed', 'repair_budget_exhausted'
                else:
                    result = runner(plan['argv'], timeout=min(REPAIR_ACTION_TIMEOUT, remaining))
                    outcome, code = result['status'], result['code']
        except Exception:
            outcome, code = 'failed', 'repair_local_failed'
        with self.db:
            self.db.execute('UPDATE wd_repairs SET outcome=?,result_code=?,lease_until=0 WHERE incident=?',
                            ('attempted' if outcome == 'ok' else outcome, code, claimed['incident']))
        return {'status': outcome, 'incident': claimed['incident'], 'action': action, 'code': code}

    def enable_analysis(self, incident_ids, now):
        """Explicit audited migration, without replaying old/closed incidents."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for iid in incident_ids:
                row = self.db.execute('SELECT * FROM wd_incidents WHERE id=?', (iid,)).fetchone()
                if self.db.execute('SELECT 1 FROM wd_policy_audit WHERE incident=?', (iid,)).fetchone():
                    continue
                if not row or row['closed'] is not None or row['decision'] != 'hard_cap_unavailable':
                    raise ValueError('policy_migration_not_eligible')
                self.db.execute('INSERT INTO wd_policy_audit VALUES (?,?,?,?)',
                                (iid, now, encoded(dict(row)), 'paid_analysis_v2'))
                self.db.execute("UPDATE wd_incidents SET decision='analysis_ready' WHERE id=?", (iid,))
            self.db.execute("INSERT OR IGNORE INTO wd_meta VALUES ('paid_analysis_v2',?)", (str(now),))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def claim_analysis(self, now):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute("SELECT * FROM wd_incidents WHERE decision='analysis_ready' AND closed IS NULL AND (healthy_since IS NULL OR EXISTS(SELECT 1 FROM wd_policy_audit WHERE incident=wd_incidents.id)) ORDER BY opened,id LIMIT 1").fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return None
            day = datetime.fromtimestamp(now, TZ).date().isoformat()
            count = self.db.execute('SELECT count(*) FROM wd_analysis WHERE day=?', (day,)).fetchone()[0]
            if count >= CAP['daily_attempts']:
                self.db.execute("UPDATE wd_incidents SET decision='analysis_daily_block' WHERE id=?", (row['id'],))
                self.db.execute('COMMIT')
                return None
            self.db.execute("INSERT INTO wd_analysis(incident,day,started,status) VALUES (?,?,?,'inflight')", (row['id'], day, now))
            self.db.execute("UPDATE wd_incidents SET decision='analysis_admitted' WHERE id=?", (row['id'],))
            self.db.execute('COMMIT')
            return dict(row)
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def expire_analysis(self, now):
        # An abandoned/unknown process never becomes retryable.
        self.db.execute("UPDATE wd_analysis SET completed=?,status='unknown',result=? WHERE completed IS NULL AND started<?",
                        (now, 'Разбор прерван или его исход неизвестен; admission сохранён, автоматического повтора нет.', now-90))

    def analyze_one(self, consumer, now):
        # Crash after admission is conservatively spent, never reclaimed.
        row = self.claim_analysis(now)
        return self.analyze_claimed(consumer, row)

    def analyze_claimed(self, consumer, row):
        if not row:
            return None
        try:
            snapshot = self.db.execute("SELECT value FROM wd_meta WHERE key='analysis_snapshot'").fetchone()
            observed = json.loads(snapshot[0]) if snapshot else {}
            row['observation'] = {'valid': observed.get('valid'), 'lane': observed.get('lanes', {}).get(row['lane'], {}),
                                  'scheduler_success': observed.get('success', 0)}
            result = consumer(row)
        except Exception:
            result = {'status': 'unknown', 'text': 'Анализатор не завершил разбор; автоматического повтора нет.', 'metadata': {}}
        status = result.get('status') if result.get('status') in ('ok', 'error', 'timeout', 'unknown') else 'unknown'
        self.db.execute('UPDATE wd_analysis SET completed=?,status=?,result=?,metadata=? WHERE incident=? AND completed IS NULL',
                        (time.time(), status, result.get('text', '')[:2400], encoded(result.get('metadata', {})), row['id']))
        return row['id']

    def publish(self, ledger):
        for row in self.db.execute('SELECT * FROM wd_incidents WHERE decision IS NOT NULL AND alert IS NULL ORDER BY opened,id').fetchall():
            self.publish_incident(ledger, row)
        for incident in self.db.execute("SELECT i.alert,i.decision,a.status FROM wd_incidents i LEFT JOIN wd_analysis a ON a.incident=i.id WHERE i.decision LIKE 'analysis_%' AND i.alert IS NOT NULL"):
            decision = 'analysis_' + incident['status'] if incident['status'] in ('ok', 'error', 'timeout', 'unknown') else incident['decision']
            with ledger.db:
                ledger.db.execute("UPDATE watchdog_notes SET decision=? WHERE error_id=? AND EXISTS(SELECT 1 FROM errors WHERE id=? AND status='pending')",
                                  (decision, incident['alert'], incident['alert']))

    def publish_incident(self, ledger, row):
        prior = self.db.execute('SELECT 1 FROM wd_incidents WHERE origin=? AND id!=? AND alert IS NOT NULL',
                                (row['origin'], row['id'])).fetchone() if row['origin'] else None
        eid = row['origin'] if row['origin'] and not prior else digest(['watchdog', row['id']])
        with ledger.db:
            ledger.db.execute('''INSERT OR IGNORE INTO errors
                (id,component,code,scope,first,last,occurrences,status)
                VALUES (?,'mail-service','watchdog_unresolved','',?,?,1,'pending')''', (eid, row['opened'], row['last']))
            ledger.db.execute('INSERT OR IGNORE INTO watchdog_notes VALUES (?,?)', (eid, row['decision']))
        self.db.execute('UPDATE wd_incidents SET alert=? WHERE id=? AND alert IS NULL', (eid, row['id']))

    def status(self):
        analyses = [dict(r) for r in self.db.execute('SELECT * FROM wd_analysis ORDER BY started DESC LIMIT 100')]
        enabled = bool(self.db.execute("SELECT 1 FROM wd_meta WHERE key='paid_analysis_v2'").fetchone())
        return {'status': 'ok', 'llm_calls': sum(bool(json.loads(r['metadata'] or '{}').get('model_verified')) for r in analyses),
                'llm_tokens': None if analyses else 0, 'actual_admissions': self.db.execute('SELECT count(*) FROM wd_analysis').fetchone()[0],
                'analysis': analyses, 'hard_token_cap': False,
                'consumer': 'incident_paid_v2' if enabled else 'disabled_hard_cap_unavailable',
                # Recovery reports what is actually permitted: bounded, allowlisted
                # repair of specific local mail disturbances. Diagnosis and repair
                # are separate; unsupported causes require a MAIN handoff.
                'recovery': 'bounded_repair' if self.repair_actions() else 'report_only',
                'supported_actions': sorted(spec['action'] for spec in REPAIR_ACTIONS.values()
                                            if spec['action'] in self.repair_actions()),
                'repairs': [dict(r) for r in self.db.execute('SELECT * FROM wd_repairs ORDER BY last DESC LIMIT 100')],
                'caps': CAP,
                'incidents': [dict(r) for r in self.db.execute('SELECT * FROM wd_incidents ORDER BY opened DESC,id LIMIT 100')],
                'days': [dict(r) for r in self.db.execute('SELECT * FROM wd_days ORDER BY day DESC LIMIT 8')]}


def main(argv=None, runner=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('check', 'status', 'enable-analysis', 'enable-repair', 'repair', 'analyze', 'check-analyze'))
    p.add_argument('--state-dir', type=Path, required=True)
    p.add_argument('--scheduler-db', type=Path)
    p.add_argument('--job-id')
    p.add_argument('--store-key')
    p.add_argument('--dispatch', action='store_true')
    p.add_argument('--repair', action='store_true')
    p.add_argument('--incident', action='append', default=[])
    p.add_argument('--action', action='append', default=[])
    p.add_argument('--analyzer-config', type=Path)
    args = p.parse_args(argv)
    if args.command in ('check', 'check-analyze') and not all((args.scheduler_db, args.job_id, args.store_key)):
        p.error('check requires exact scheduler-db, job-id and store-key')
    if args.command == 'status' and args.dispatch:
        p.error('status cannot dispatch')
    if args.command in ('analyze', 'check-analyze') and not args.analyzer_config:
        p.error('analyze requires analyzer-config')
    def deadline(signum, frame):
        raise TimeoutError('watchdog_deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(100 if args.command in ('analyze', 'check-analyze') else 25)
    repair_result = {'status': 'disabled'}
    try:
        with closing(Watchdog(args.state_dir / 'watchdog.sqlite3')) as watcher:
            if args.command == 'enable-analysis':
                watcher.enable_analysis(args.incident, time.time())
                with closing(Ledger(args.state_dir)) as ledger:
                    watcher.publish(ledger)
                delivery = {'status': 'disabled'}
            elif args.command == 'enable-repair':
                watcher.enable_repair(args.action, time.time())
                with closing(Ledger(args.state_dir)) as ledger:
                    watcher.publish(ledger)
                delivery = {'status': 'disabled'}
            elif args.command == 'repair':
                # One bounded allowlisted action only. No observation, alert
                # projection, analyzer, model or scheduler mutation occurs here.
                delivery = {'status': 'disabled'}
                repaired = watcher.repair(time.time(), runner)
                out = watcher.status()
                out['delivery'] = delivery
                out['repair'] = repaired
                print(encoded(out))
                return 0 if out['status'] == 'ok' else 1
            elif args.command in ('analyze', 'check-analyze'):
                analyzer = sibling_module('analyzer')
                Consumer, deliver = analyzer.Consumer, analyzer.deliver
                consumer = Consumer(args.analyzer_config, args.state_dir)
                if args.command == 'check-analyze':
                    watcher.observe(collect(args.state_dir, args.scheduler_db, args.job_id, args.store_key, time.time()), time.time())
                watcher.expire_analysis(time.time())
                # Resolve admission before alert projection, but alert before the
                # slow consumer. A crash keeps the unique admission spent.
                claimed = watcher.claim_analysis(time.time())
                repaired = {'status': 'disabled'}
                with closing(Ledger(args.state_dir)) as ledger:
                    watcher.publish(ledger)
                    if args.repair:
                        repaired = watcher.repair(time.time(), runner)
                    if args.command == 'check-analyze' and args.dispatch:
                        ledger.dispatch_error(sibling_module('router').Telegram())
                    watcher.analyze_claimed(consumer, claimed)
                    watcher.publish(ledger)  # Only pending notes may advance.
                delivery = deliver(watcher, consumer, args.dispatch)
                repair_result = repaired
            elif args.command == 'check':
                watcher.observe(collect(args.state_dir, args.scheduler_db, args.job_id, args.store_key, time.time()), time.time())
                repair_result = {'status': 'disabled'}
                with closing(Ledger(args.state_dir)) as ledger:
                    watcher.publish(ledger)
                    if args.repair:
                        repair_result = watcher.repair(time.time(), runner)
                    if args.dispatch:
                        delivery = ledger.dispatch_error(sibling_module('router').Telegram())
                    else:
                        delivery = {'status': 'disabled'}
            else:
                delivery = {'status': 'disabled'}
            out = watcher.status()
            out['delivery'] = delivery
            out['repair'] = repair_result
    except (OSError, sqlite3.Error, ValueError, TimeoutError):
        # No recursive report-error on observer/analyzer failure.
        out = {'status': 'error', 'code': 'watchdog_unavailable', 'llm_calls': 0, 'llm_tokens': 0}
    finally:
        signal.alarm(0)
    print(encoded(out))
    return 0 if out['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
