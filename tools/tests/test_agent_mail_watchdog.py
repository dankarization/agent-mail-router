"""Failure-oriented incident admission proofs, offline metadata and fake delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

import agent_mail_watchdog as w
from agent_mail_lifecycle import Ledger
from test_agent_mail_router import DAY, NIGHT


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.path = self.state / 'watchdog.sqlite3'
        self.w = w.Watchdog(self.path)
        self.ledger = Ledger(self.state)
        self.now = DAY.timestamp()
        self.scheduler_path = self.root / 'scheduler.sqlite3'
        with sqlite3.connect(self.scheduler_path) as db:
            db.execute('CREATE TABLE cron_jobs(job_id TEXT,store_key TEXT,enabled INTEGER,job_json TEXT,state_json TEXT)')
            db.execute('INSERT INTO cron_jobs VALUES (?,?,?,?,?)', ('job', 'store', 1,
                       json.dumps({'schedule': {'everyMs': 900000}, 'payload': 'PRIVATE_BODY'}), '{}'))
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            db.execute("CREATE TABLE effects(id TEXT,kind TEXT,status TEXT,target TEXT,receipt TEXT,summary_id TEXT DEFAULT 'summary')")
            db.execute('CREATE TABLE batches(id TEXT,result TEXT)')
            db.execute('CREATE TABLE mail(payload TEXT)')
            db.execute("INSERT INTO mail VALUES ('PRIVATE_MAIL')")
        self.job(lastRunAtMs=(self.now-60)*1000, lastRunStatus='ok', consecutiveErrors=0)

    def tearDown(self):
        self.w.close()
        self.ledger.close()
        self.tmp.cleanup()

    def job(self, **state):
        with sqlite3.connect(self.scheduler_path) as db:
            db.execute('UPDATE cron_jobs SET state_json=?', (json.dumps(state),))

    def collect(self, now=None):
        return w.collect(self.state, self.scheduler_path, 'job', 'store', self.now if now is None else now)

    def snapshot(self, lanes=None, errors=None, success=0, valid=True):
        return {'valid': valid, 'lanes': lanes or {}, 'errors': errors or [], 'success': success}

    def bad(self, event='a', lane='scheduler'):
        return self.snapshot({lane: {'code': 'fixed_failure', 'event': event}})

    def baseline(self):
        self.w.observe(self.snapshot(), self.now)

    def admit(self, lane='scheduler', start=None):
        start = self.now + 1 if start is None else start
        self.w.observe(self.bad(lane=lane), start)
        return self.w.observe(self.bad(lane=lane), start + 301)

    def test_healthy_no_calls_and_no_incidents(self):
        self.w.observe(self.collect(), self.now)
        for n in range(1, 10):
            result = self.w.observe(self.collect(), self.now+n)
        self.assertEqual(result['llm_calls'], 0)
        self.assertEqual(result['llm_tokens'], 0)
        self.assertEqual(result['incidents'], [])
        self.assertEqual(result['days'], [])

    def test_historical_error_baseline_no_replay(self):
        self.ledger.error('mail-monitor', 'old_failure', now=self.now-10000)
        self.w.observe(self.collect(), self.now)
        self.w.observe(self.collect(), self.now+500)
        self.assertEqual(self.w.status()['incidents'], [])

    def test_stale_error_counter_bump_after_blind_gap_is_not_a_new_incident(self):
        # #53971: the observer was blind across a Gateway restart and the stored
        # counter advanced for an occurrence that was already ~13h old when first
        # seen. A stale bump must not open (and re-deliver) a fresh incident.
        stale = self.now - 13 * 3600
        self.baseline()
        self.w.observe(self.snapshot(errors=[{'id': 'old', 'component': 'mail-monitor',
                                              'occurrences': 9, 'last': stale, 'status': 'sent'}]), self.now)
        out = self.w.observe(self.snapshot(errors=[{'id': 'old', 'component': 'mail-monitor',
                                                    'occurrences': 10, 'last': stale, 'status': 'sent'}]), self.now+300)
        self.assertEqual(out['incidents'], [])

    def test_recent_error_counter_bump_still_opens_incident(self):
        # A genuinely recent occurrence keeps the original runtime_failure path.
        self.baseline()
        self.w.observe(self.snapshot(errors=[{'id': 'new', 'component': 'mail-monitor',
                                              'occurrences': 9, 'last': self.now, 'status': 'sent'}]), self.now)
        out = self.w.observe(self.snapshot(errors=[{'id': 'new', 'component': 'mail-monitor',
                                                    'occurrences': 10, 'last': self.now, 'status': 'sent'}]), self.now+300)
        self.assertEqual(len(out['incidents']), 1)
        self.assertEqual(out['incidents'][0]['origin'], 'new')
        self.assertEqual(out['incidents'][0]['code'], 'runtime_failure')

    def test_error_without_freshness_metadata_never_opens_incident(self):
        # Missing freshness metadata fails closed: a counter bump alone cannot
        # prove a recent occurrence, so it must not open an incident.
        self.baseline()
        self.w.observe(self.snapshot(errors=[{'id': 'n', 'component': 'mail-monitor',
                                              'occurrences': 1, 'last': None, 'status': 'sent'}]), self.now)
        out = self.w.observe(self.snapshot(errors=[{'id': 'n', 'component': 'mail-monitor',
                                                    'occurrences': 2, 'last': None, 'status': 'sent'}]), self.now+300)
        self.assertEqual(out['incidents'], [])

    def test_old_run_error_with_new_successful_trigger_is_healthy(self):
        self.job(lastRunAtMs=(self.now-90000)*1000, lastRunStatus='error',
                 lastTriggerEvalAtMs=(self.now-5)*1000, consecutiveErrors=0)
        self.assertEqual(self.collect()['lanes']['scheduler']['code'], '')

    def test_recent_execution_model_or_tool_failure_observed(self):
        self.baseline()
        self.job(lastRunAtMs=self.now*1000, lastRunStatus='error',
                 lastTriggerEvalAtMs=(self.now+1)*1000, consecutiveErrors=1)
        observed = self.collect()
        self.assertEqual(observed['lanes']['scheduler']['code'], 'scheduler_execution_failed')
        self.w.observe(observed, self.now+1)
        out = self.w.observe(observed, self.now+302)
        self.assertEqual(out['incidents'][0]['decision'], 'hard_cap_unavailable')

    def test_repeat_changed_fingerprint_no_second_admission(self):
        self.baseline(); self.admit()
        for n in range(5):
            self.w.observe(self.bad(str(n)), self.now+400+n)
        out = self.w.status()
        self.assertEqual(len(out['incidents']), 1)
        self.assertEqual(out['days'][0]['decisions'], 1)
        self.assertEqual(out['incidents'][0]['repeats'], 7)

    def test_transient_recovers_without_reservation(self):
        self.baseline()
        self.w.observe(self.bad(), self.now+1)
        self.w.observe(self.snapshot(), self.now+100)
        out = self.w.observe(self.snapshot(), self.now+1001)
        self.assertIsNotNone(out['incidents'][0]['closed'])
        self.assertIsNone(out['incidents'][0]['decision'])
        self.assertEqual(out['days'], [])

    def test_recurrence_requires_sustained_recovery(self):
        self.baseline(); self.admit()
        self.w.observe(self.snapshot(), self.now+400)
        self.w.observe(self.bad('changed'), self.now+1200)
        self.assertEqual(len(self.w.status()['incidents']), 1)
        self.w.observe(self.snapshot(), self.now+1300)
        self.w.observe(self.snapshot(), self.now+2200)
        out = self.admit(start=self.now+2300)
        self.assertEqual(len(out['incidents']), 2)
        self.assertEqual(out['days'][0]['decisions'], 2)

    def test_daily_caps_global_across_fingerprints_and_components(self):
        self.baseline()
        for lane in ('scheduler','observer','mail-monitor','mail-delivery'):
            self.admit(lane)
        out = self.w.status()
        self.assertEqual(out['days'][0]['tokens'], 16000)
        self.assertEqual(out['days'][0]['decisions'], 2)
        self.assertEqual(sum(r['decision']=='daily_budget_block' for r in out['incidents']), 2)
        self.assertEqual(out['llm_tokens'], 0)

    def test_tbilisi_midnight_rollover_no_replay(self):
        self.baseline()
        before = datetime(2026, 10, 24, 23, 59, tzinfo=w.TZ).timestamp()
        self.admit('scheduler', before-400)
        self.admit('observer', before-400)
        after = before+120
        self.admit('mail-monitor', after)
        out = self.w.status()
        self.assertEqual([d['tokens'] for d in out['days']], [8000,16000])
        self.w.observe(self.bad(), after+1000)
        self.assertEqual(sum(d['decisions'] for d in self.w.status()['days']), 3)

    def test_restart_terminal_decision_not_retried(self):
        self.baseline(); self.admit()
        self.w.close(); self.w = w.Watchdog(self.path)
        out = self.w.observe(self.bad(), self.now+50000)
        self.assertEqual(out['days'][0]['decisions'], 1)
        self.assertEqual(out['llm_calls'], 0)

    def test_concurrent_admission_single_reservation(self):
        self.baseline()
        self.w.observe(self.bad(), self.now+1)
        def race(_):
            with closing(w.Watchdog(self.path)) as watcher:
                return watcher.observe(self.bad(), self.now+302)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(race, range(8)))
        self.assertEqual(self.w.status()['days'][0]['decisions'], 1)
        self.assertEqual(len(self.w.status()['incidents']), 1)

    def test_transaction_crash_rollback_and_restart(self):
        self.baseline()
        self.w.observe(self.bad(), self.now+1)
        self.w.db.execute("CREATE TRIGGER fail BEFORE UPDATE OF decision ON wd_incidents BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.Error):
            self.w.observe(self.bad(), self.now+302)
        self.assertEqual(self.w.status()['days'], [])
        self.w.db.execute('DROP TRIGGER fail')
        self.assertEqual(self.w.observe(self.bad(), self.now+303)['days'][0]['tokens'],8000)

    def test_unknown_or_timeout_terminal_attempt_stays_spent(self):
        self.baseline(); self.admit()
        self.w.db.execute("UPDATE wd_incidents SET decision='analysis_unknown'")
        self.w.close(); self.w = w.Watchdog(self.path)
        out = self.w.observe(self.bad('new_fingerprint'), self.now+100000)
        self.assertEqual(out['incidents'][0]['decision'],'analysis_unknown')
        self.assertEqual(sum(d['decisions'] for d in out['days']),1)

    def test_new_runtime_error_reaches_block_then_independent_success_closes(self):
        self.baseline()
        eid=self.ledger.error('mail-monitor','poll_failed',now=self.now+1)
        self.w.observe(self.collect(), self.now+1)
        out=self.w.observe(self.collect(), self.now+302)
        incident=out['incidents'][0]
        self.assertEqual(incident['origin'],eid)
        self.assertEqual(incident['decision'],'hard_cap_unavailable')
        self.w.publish(self.ledger)
        # Marking the alert sent is NOT resolution.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE errors SET status='sent',receipt='42'")
        self.assertIsNone(self.w.observe(self.collect(),self.now+400)['incidents'][0]['closed'])
        self.job(lastRunAtMs=(self.now+500)*1000,lastRunStatus='ok',consecutiveErrors=0)
        self.w.observe(self.collect(),self.now+501)
        self.assertIsNotNone(self.w.observe(self.collect(),self.now+1401)['incidents'][0]['closed'])

    def test_expected_held_semantic_queue_and_unresolved_alert_do_not_repeat(self):
        self.baseline()
        self.ledger.error('mail-service', 'watchdog_unresolved', now=self.now+1)
        held = {'version':1,'status':'held','outcome':'semantic_processing_unavailable'}
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT INTO meta VALUES (?,?)', ('semantic_queue_v1:test', json.dumps(held)))
        for offset in (302, 602, 902, 1202):
            observed = self.collect(self.now+offset)
            self.assertEqual(observed['errors'], [])
            out = self.w.observe(observed, self.now+offset)
            self.w.publish(self.ledger)
            self.assertEqual(out['incidents'], [])
        self.assertEqual(len(self.ledger.status()['errors']), 1)
        self.assertEqual(self.ledger.status()['errors'][0]['code'], 'watchdog_unresolved')
        self.assertEqual(self.ledger.status()['errors'][0]['occurrences'], 1)
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('SELECT value FROM meta WHERE key=?',
                ('semantic_queue_v1:test',)).fetchone()[0]), held)

    def test_unknown_delivery_held_and_metadata_unchanged(self):
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute("INSERT INTO effects(id,kind,status,target,receipt) VALUES ('effect','notify','unknown','PRIVATE_TARGET','PRIVATE_RECEIPT')")
            before=list(db.execute('SELECT * FROM effects'))
        self.baseline()
        observed=self.collect()
        self.w.observe(observed,self.now+1)
        self.w.observe(observed,self.now+302)
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            self.assertEqual(before,list(db.execute('SELECT * FROM effects')))
        text=json.dumps(observed)
        self.assertNotIn('PRIVATE',text)

    def test_quiet_hours_and_single_notification(self):
        self.baseline(); self.admit(); self.w.publish(self.ledger)
        tg=Mock();tg.send.return_value={'status':'sent','receipt':'42'}
        self.ledger.dispatch_error(tg,lambda:NIGHT)
        tg.send.assert_not_called()
        self.ledger.dispatch_error(tg,lambda:DAY)
        self.w.publish(self.ledger)
        self.ledger.dispatch_error(tg,lambda:DAY)
        self.assertEqual(tg.send.call_count,1)
        self.assertIn('LLM вызовов: 0',tg.send.call_args.args[1])
        self.assertIn('инцидент не устранён',tg.send.call_args.args[1])

    def test_unknown_notification_never_retried(self):
        self.baseline();self.admit();self.w.publish(self.ledger)
        tg=Mock();tg.send.side_effect=TimeoutError('PRIVATE_EXCEPTION')
        self.ledger.dispatch_error(tg,lambda:DAY)
        self.w.publish(self.ledger)
        self.ledger.dispatch_error(tg,lambda:DAY)
        self.assertEqual(tg.send.call_count,1)
        self.assertEqual(self.ledger.status()['errors'][0]['status'],'unknown')

    def test_publish_crash_between_databases_idempotent(self):
        self.baseline();self.admit();self.w.publish(self.ledger)
        self.w.db.execute('UPDATE wd_incidents SET alert=NULL')
        self.w.publish(self.ledger)
        self.assertEqual(len(self.ledger.status()['errors']),1)
        self.assertEqual(self.ledger.status()['errors'][0]['occurrences'],1)

    def test_runtime_error_original_slot_reused_no_duplicate(self):
        self.baseline()
        self.ledger.error('mail-delivery','telegram_ack_unknown',now=self.now+1)
        observed=self.collect()
        self.w.observe(observed,self.now+1);self.w.observe(observed,self.now+302)
        self.w.publish(self.ledger)
        self.assertEqual(len(self.ledger.status()['errors']),1)

    def test_watchdog_notification_failure_no_recursive_incident(self):
        self.baseline();self.admit();self.w.publish(self.ledger)
        out=self.w.observe(self.collect(),self.now+400)
        self.assertEqual(len(out['incidents']),1)

    def test_partial_schema_failure_not_false_recovery(self):
        self.baseline();self.admit()
        with sqlite3.connect(self.scheduler_path) as db:
            db.execute("UPDATE cron_jobs SET state_json='{' ")
        out=self.collect()
        self.assertFalse(out['valid'])
        self.w.observe(out,self.now+400)
        self.w.observe(out,self.now+2000)
        self.assertTrue(all(r['closed'] is None for r in self.w.status()['incidents']))

    def test_source_query_only_rejects_writes(self):
        with closing(w.readonly(self.scheduler_path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM cron_jobs')

    def test_missing_source_never_created(self):
        missing=self.root/'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            w.readonly(missing)
        self.assertFalse(missing.exists())

    def test_bounded_rows_fail_closed(self):
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.executemany("INSERT INTO effects(id,kind,status,target,receipt) VALUES (?,'context','unknown','SECRET','SECRET')",[(str(i),) for i in range(w.MAX_ROWS+1)])
        self.assertFalse(self.collect()['valid'])

    def test_pending_summary_detected_without_payload(self):
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute("INSERT INTO batches VALUES ('batch',NULL)")
        self.assertEqual(self.collect()['lanes']['mail-monitor']['code'],'summary_pending')

    def test_night_pending_notify_is_normal(self):
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute("INSERT INTO effects(id,kind,status,target,receipt) VALUES ('effect','notify','pending','PRIVATE','PRIVATE')")
        self.assertEqual(self.collect(NIGHT.timestamp())['lanes']['mail-delivery']['code'],'')

    def test_scheduler_exact_store_identity(self):
        out=w.collect(self.state,self.scheduler_path,'job','other-store',self.now)
        self.assertFalse(out['valid'])

    def test_no_executable_analysis_or_arbitrary_consumer_hook(self):
        import ast
        tree=ast.parse(Path(w.__file__).read_text())
        imports={n.names[0].name for n in ast.walk(tree) if isinstance(n,ast.Import)}
        self.assertFalse(imports & {'subprocess','urllib','socket','http','requests'})
        self.assertEqual(w.CAP['total_tokens'],8000)
        self.assertEqual(w.CAP['read_only_tools'],4)
        self.assertEqual(w.CAP['seconds'],60)

    def test_failed_initial_observation_cannot_replay_historical_errors(self):
        self.w.observe(self.snapshot({'observer': {'code':'unavailable','event':'x'}},valid=False),self.now)
        self.ledger.error('mail-content','old_failure',now=self.now-10000)
        self.w.observe(self.collect(),self.now+10)
        self.w.observe(self.collect(),self.now+20)
        self.assertFalse(any(r['lane']=='mail-content' for r in self.w.status()['incidents']))

    def test_observation_gap_breaks_recovery_interval(self):
        self.baseline();self.admit()
        self.w.observe(self.snapshot(),self.now+400)
        self.w.observe(self.snapshot(valid=False),self.now+1000)
        out=self.w.observe(self.snapshot(),self.now+1400)
        self.assertIsNone(out['incidents'][0]['closed'])
        out=self.w.observe(self.snapshot(),self.now+2300)
        self.assertIsNotNone(out['incidents'][0]['closed'])

    def test_corrupt_metadata_is_not_leaked(self):
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO errors(id,component,code,scope,first,last,occurrences,status) VALUES ('SECRET_ID','mail-monitor','fixed','',0,0,1,'pending')")
        out=self.collect()
        self.assertFalse(out['valid'])
        self.assertNotIn('SECRET',json.dumps(out))

    def test_cli_healthy_baseline_preserves_source_rows(self):
        import io
        from contextlib import redirect_stdout
        before={}
        for file in ('mail.sqlite3','lifecycle.sqlite3'):
            with closing(w.readonly(self.state/file)) as db:
                before[file]=list(db.execute('SELECT * FROM '+('mail' if file.startswith('mail.') else 'errors')))
        output=io.StringIO()
        with redirect_stdout(output):
            code=w.main(['check','--state-dir',str(self.state),'--scheduler-db',str(self.scheduler_path),
                         '--job-id','job','--store-key','store'])
        self.assertEqual(code,0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['delivery']['status'],'disabled')
        self.assertEqual(result['llm_tokens'],0)
        for file in before:
            with closing(w.readonly(self.state/file)) as db:
                self.assertEqual(before[file],list(db.execute('SELECT * FROM '+('mail' if file.startswith('mail.') else 'errors'))))

    def test_cli_timeout_has_no_recursive_alert(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        output=io.StringIO()
        with patch.object(w,'collect',side_effect=TimeoutError('SECRET_RAW_EXCEPTION')),redirect_stdout(output):
            code=w.main(['check','--state-dir',str(self.state),'--scheduler-db',str(self.scheduler_path),
                         '--job-id','job','--store-key','store'])
        self.assertEqual(code,1)
        self.assertNotIn('SECRET',output.getvalue())
        self.assertEqual(self.ledger.status()['errors'],[])

    def test_inflight_notification_no_blind_resend(self):
        self.baseline();self.admit();self.w.publish(self.ledger)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE errors SET status='inflight'")
        tg=Mock()
        self.w.publish(self.ledger)
        self.ledger.dispatch_error(tg,lambda:DAY)
        tg.send.assert_not_called()

    def test_successful_poll_does_not_resolve_content_failure(self):
        self.baseline()
        self.ledger.error('mail-content','part_limit',now=self.now+1)
        self.w.observe(self.collect(),self.now+1)
        self.job(lastRunAtMs=(self.now+500)*1000,lastRunStatus='ok',consecutiveErrors=0)
        self.w.observe(self.collect(),self.now+600)
        out=self.w.observe(self.collect(),self.now+1800)
        self.assertIsNone(out['incidents'][0]['closed'])
