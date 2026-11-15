"""Bounded allowlisted repair: classification, limits, verification, no blind retry."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import unittest

import agent_mail_watchdog as w
from agent_mail_lifecycle import Ledger
from test_agent_mail_watchdog import WatchdogTests


class RepairTests(WatchdogTests):
    setUp = WatchdogTests.setUp
    tearDown = WatchdogTests.tearDown

    def open_incident(self, lane, code, iid=None, opened=None):
        iid = iid or (lane[0] * 32)
        opened = self.now - 600 if opened is None else opened
        with self.w.db:
            self.w.db.execute('DELETE FROM wd_incidents WHERE lane=?', (lane,))
            self.w.db.execute('INSERT INTO wd_incidents(id,lane,generation,opened,last,event,repeats,origin,code) '
                              'VALUES (?,?,1,?,?,?,1,NULL,?)',
                              (iid, lane, opened, opened, 'event-' + code, code))
        return iid

    def runner(self, calls, result=None):
        def run(argv, timeout=None):
            calls.append((list(argv), timeout))
            return result or {'status': 'ok', 'code': 'repair_dispatched'}
        return run

    def spent_day(self):
        day = w.datetime.fromtimestamp(self.now, w.TZ).date().isoformat()
        for n in range(2):
            self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status) VALUES (?,?,?,?,'unknown')",
                              (str(n), day, self.now - 10, self.now - 1))

    def test_disabled_by_default_is_report_only_and_never_acts(self):
        self.open_incident('observer', 'error_journal_unreplayed')
        calls = []
        self.assertEqual(self.w.repair(self.now, self.runner(calls)), {'status': 'idle'})
        self.assertEqual(calls, [])
        self.assertEqual(self.w.status()['recovery'], 'report_only')
        self.assertEqual(self.w.status()['supported_actions'], [])
        self.assertEqual(self.w.status()['repairs'], [])

    def test_activation_is_explicit_and_named_only(self):
        with self.assertRaises(ValueError):
            self.w.enable_repair(['self_edit_code'], self.now)
        self.w.enable_repair(['replay_error_journal'], self.now)
        status = self.w.status()
        self.assertEqual(status['recovery'], 'bounded_repair')
        self.assertEqual(status['supported_actions'], ['replay_error_journal'])

    def test_unclassified_legacy_row_is_never_repaired(self):
        self.w.enable_repair(['apply_seen_once'], self.now)
        iid = 'b' * 32
        with self.w.db:
            self.w.db.execute('INSERT INTO wd_incidents(id,lane,generation,opened,last,event,repeats,origin,code) '
                              'VALUES (?,?,1,?,?,?,1,NULL,NULL)', (iid, 'mail-seen', self.now - 600, self.now - 600, 'legacy'))
        self.ledger.intent('77:5', {'proof': 'p'})
        self.assertEqual(self.w.repair(self.now)['status'], 'idle')

    def test_unsupported_code_gives_handoff_not_action(self):
        self.w.enable_repair(['replay_error_journal'], self.now)
        self.open_incident('scheduler', 'scheduler_execution_failed')
        self.assertEqual(self.w.repair(self.now)['status'], 'idle')
        self.assertEqual(self.w.status()['repairs'], [])

    def test_local_journal_repair_ok_then_independent_health_verifies(self):
        self.w.enable_repair(['replay_error_journal'], self.now)
        self.baseline()
        iid = self.open_incident('observer', 'error_journal_unreplayed')
        out = self.w.repair(self.now)
        self.assertEqual((out['status'], out['code']), ('ok', 'repair_error_journal_replayed'))
        self.assertEqual(self.w.status()['repairs'][0]['outcome'], 'attempted')
        # A start is not success: only a later ordinary healthy observation verifies.
        self.assertIsNone(self.w.status()['repairs'][0]['verified'])
        self.w.observe(self.snapshot(), self.now + 1)
        self.w.observe(self.snapshot(), self.now + 1002)
        row = self.w.status()['repairs'][0]
        self.assertEqual(row['outcome'], 'recovered')
        self.assertIsNotNone(row['verified'])
        self.assertIsNotNone(self.w.status()['incidents'][0]['closed'])

    def test_delegate_candidate_requires_absence_of_uncertain_intent(self):
        self.w.enable_repair(['dispatch_pending_effect'], self.now)
        self.baseline()
        self.open_incident('mail-delivery', 'mail-delivery_overdue')
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            db.execute("INSERT INTO effects VALUES ('e1','notify','pending','t',NULL,'s')")
        calls = []
        self.assertEqual(self.w.repair(self.now, self.runner(calls))['status'], 'ok')
        self.assertEqual(len(calls), 1)
        self.assertIn('dispatch', calls[0][0])
        # An uncertain intent blocks any action (never a blind replay).
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            db.execute("UPDATE effects SET status='unknown' WHERE id='e1'")
            db.execute("INSERT INTO effects VALUES ('e2','notify','pending','t',NULL,'s')")
        self.open_incident('mail-delivery', 'mail-delivery_overdue', opened=self.now + 300)
        out = self.w.repair(self.now + 400, self.runner(calls))
        self.assertEqual((out['status'], out['code']), ('skipped', 'no_safe_pending_candidate'))
        self.assertEqual(len(calls), 1)
        row = self.w.status()['repairs'][0]
        self.assertEqual((row['outcome'], row['result_code']), ('skipped', 'no_safe_pending_candidate'))
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects WHERE status='pending'").fetchone()[0], 1)
        self.assertEqual(self.ledger.status()['errors'], [])

    def test_watchdog_short_dispatch_budget_defers_without_child_or_intent(self):
        from unittest.mock import patch
        import agent_mail_repair as executor
        self.w.enable_repair(['dispatch_pending_effect'], self.now)
        self.open_incident('mail-delivery', 'mail-delivery_overdue')
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            db.execute("INSERT INTO effects VALUES ('e1','context','pending','t',NULL,'s')")
        with patch.object(w.time, 'monotonic', return_value=100), patch.object(executor.subprocess, 'run') as launch:
            out = self.w.repair(self.now, executor.run, budget=2)
        self.assertEqual((out['status'], out['code']), ('deferred', 'repair_budget_exhausted'))
        launch.assert_not_called()
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            self.assertEqual(db.execute("SELECT status FROM effects WHERE id='e1'").fetchone()[0], 'pending')
        self.assertEqual(self.w.status()['repairs'][0]['outcome'], 'deferred')
        self.assertEqual(self.w.repair(self.now + 1, self.runner([]))['status'], 'idle')
        calls = []
        self.assertEqual(self.w.repair(self.now + 400, self.runner(calls))['status'], 'ok')
        self.assertEqual(len(calls), 1)

    def test_unknown_outcome_is_never_retried(self):
        self.w.enable_repair(['apply_seen_once'], self.now)
        self.open_incident('mail-seen', 'mail-seen_held')
        self.ledger.intent('77:7', {'proof': 'p'})
        calls = []
        first = self.w.repair(self.now, self.runner(calls, {'status': 'unknown', 'code': 'repair_action_timeout'}))
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(self.w.status()['repairs'][0]['outcome'], 'unknown')
        self.assertEqual(self.w.repair(self.now + 5000, self.runner(calls))['status'], 'idle')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.w.status()['repairs'][0]['attempts'], 1)

    def test_attempt_cap_and_backoff_limit_failed_actions(self):
        self.w.enable_repair(['dispatch_pending_effect'], self.now)
        self.open_incident('mail-delivery', 'mail-delivery_overdue')
        with sqlite3.connect(self.state / 'mail.sqlite3') as db:
            db.execute("INSERT INTO effects VALUES ('e1','context','pending','t',NULL,'s')")
        calls = []
        fail = self.runner(calls, {'status': 'failed', 'code': 'repair_action_failed'})
        self.assertEqual(self.w.repair(self.now, fail)['status'], 'failed')
        self.assertEqual(self.w.repair(self.now + 1, fail)['status'], 'idle')
        self.assertEqual(self.w.repair(self.now + 400, fail)['status'], 'failed')
        self.assertEqual(self.w.repair(self.now + 5000, fail)['status'], 'idle')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.w.status()['repairs'][0]['attempts'], 2)

    def test_lease_blocks_concurrent_repair_connection(self):
        self.w.enable_repair(['replay_error_journal'], self.now)
        self.open_incident('observer', 'error_journal_unreplayed')
        calls = []
        self.assertEqual(self.w.repair(self.now, self.runner(calls))['status'], 'ok')
        second = w.Watchdog(self.path)
        try:
            self.assertEqual(second.repair(self.now + 5, self.runner(calls))['status'], 'idle')
        finally:
            second.close()
        self.assertEqual(len(calls), 0)

    def test_repair_does_not_touch_admissions_cursor_or_mail(self):
        self.w.enable_analysis([], self.now)
        self.baseline()
        self.spent_day()
        day_before = [tuple(r) for r in self.w.db.execute('SELECT * FROM wd_days')]
        with self.w.db:
            self.w.db.execute("INSERT OR REPLACE INTO wd_meta VALUES ('cursor','{\"last_uid\":98}')")
        self.w.enable_repair(['replay_error_journal'], self.now)
        self.open_incident('observer', 'error_journal_unreplayed')
        self.w.repair(self.now)
        self.assertEqual(self.w.status()['actual_admissions'], 2)
        self.assertEqual(day_before, [tuple(r) for r in self.w.db.execute('SELECT * FROM wd_days')])
        self.assertEqual(json.loads(self.w.db.execute("SELECT value FROM wd_meta WHERE key='cursor'").fetchone()[0]),
                         {'last_uid': 98})
        self.assertEqual(self.ledger.status()['errors'], [])

    def test_cli_repair_runs_one_action_and_reports_status(self):
        import io
        from contextlib import redirect_stdout
        self.w.close()
        w.main(['enable-repair', '--state-dir', str(self.state), '--action', 'replay_error_journal'])
        probe = w.Watchdog(self.path)
        iid = 'c' * 32
        with probe.db:
            probe.db.execute('INSERT INTO wd_incidents(id,lane,generation,opened,last,event,repeats,origin,code) '
                             'VALUES (?,?,1,?,?,?,1,NULL,?)',
                             (iid, 'observer', self.now - 600, self.now - 600, 'event', 'error_journal_unreplayed'))
        probe.close()
        output = io.StringIO()
        with redirect_stdout(output):
            code = w.main(['repair', '--state-dir', str(self.state)])
        out = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(out['recovery'], 'bounded_repair')
        self.assertEqual(out['repair']['code'], 'repair_error_journal_replayed')
        self.assertEqual(out['delivery'], {'status': 'disabled'})
        self.assertEqual(out['llm_calls'], 0)


class SiblingImportTests(unittest.TestCase):
    def test_deployed_prefix_resolves_analyzer_and_router(self):
        self.assertEqual(w.sibling_stem('agent_mail_watchdog.py', 'analyzer'), 'agent_mail_analyzer')
        self.assertEqual(w.sibling_stem('syncopia_agent_mail_watchdog.py', 'analyzer'), 'syncopia_agent_mail_analyzer')
        self.assertEqual(w.sibling_stem('syncopia_agent_mail_watchdog.py', 'router'), 'syncopia_agent_mail_router')
        self.assertEqual(w.sibling_stem('agent_mail_watchdog.py', 'lifecycle'), 'agent_mail_lifecycle')


class ExecutorBoundaryTests(unittest.TestCase):
    """The separate executor re-validates argv; a corrupt plan is not a command."""

    def setUp(self):
        import agent_mail_repair as r
        self.r = r
        self.python = r.sys.executable

    def test_exact_allowlisted_shapes_only(self):
        prefix, state = '/state/mail-routing', '/state/mail-routing'
        self.assertEqual(self.r.validate([self.python, 'tools/agent_mail_router.py', '--state-dir', state, 'dispatch']), 'dispatch')
        self.assertEqual(self.r.validate([self.python, 'syncopia_agent_mail_router.py', '--state-dir', state, 'dispatch']), 'dispatch')
        self.assertEqual(self.r.validate([self.python, 'agent_mail_content.py', '--state-dir', state, 'seen', '--key', '77:9', '--apply']), 'seen')
        self.assertEqual(self.r.validate([self.python, 'syncopia_agent_mail_content.py', '--state-dir', state, 'seen', '--key', '77:1042', '--apply']), 'seen')
        self.assertEqual(self.r.validate([self.python, 'agent_mail_router.py', '--state-dir', prefix, 'dispatch']), 'dispatch')

    def test_dispatch_short_budget_defers_without_starting_subprocess(self):
        from unittest.mock import patch
        argv = [self.python, 'agent_mail_router.py', '--state-dir', '/state/mail-routing', 'dispatch']
        with patch.object(self.r.subprocess, 'run') as launch:
            for timeout in (0, 1, 2):
                with self.subTest(timeout=timeout):
                    self.assertEqual(self.r.run(argv, timeout=timeout),
                                     {'status': 'deferred', 'code': 'repair_budget_exhausted'})
            launch.assert_not_called()

    def test_dispatch_child_receives_budget_strictly_inside_parent_timeout(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        argv = [self.python, "agent_mail_router.py", "--state-dir", "/state/mail-routing", "dispatch"]
        with patch.object(self.r.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b'{\"status\":\"deferred\"}')) as run:
            self.r.run(argv, timeout=11)
        child = run.call_args.args[0]
        self.assertEqual(child[:len(argv)], argv)
        self.assertEqual(child[-2], "--deadline-monotonic")
        self.assertLessEqual(float(child[-1]) - self.r.time.monotonic(), 9)
        self.assertEqual(run.call_args.kwargs["timeout"], 11)

    def test_shell_arbitrary_and_near_miss_argv_are_rejected(self):
        state = '/state/mail-routing'
        rejected = [
            [], ['/bin/sh', '-c', 'id'],
            [self.python, 'agent_mail_router.py', '--state-dir', state, 'tick'],
            [self.python, 'agent_mail_router.py', 'dispatch', '--state-dir', state],
            [self.python, 'agent_mail_router.py', '--state-dir', state, 'dispatch', '--force'],
            [self.python, 'agent_mail_content.py', '--state-dir', state, 'seen', '--key', '77:9'],
            [self.python, 'agent_mail_content.py', '--state-dir', state, 'seen', '--key', '0:9', '--apply'],
            [self.python, 'agent_mail_content.py', '--state-dir', state, 'seen', '--key', '77:9', 'send'],
            [self.python, 'other_tool.py', '--state-dir', state, 'dispatch'],
            [self.python, 'agent_mail_router.py', '--state-dir', 'relative', 'dispatch'],
            'not-a-list',
        ]
        for argv in rejected:
            self.assertIsNone(self.r.validate(argv), repr(argv))

    def test_rejected_argv_returns_a_code_without_launching_anything(self):
        self.assertEqual(self.r.main(['run', '["/bin/sh","-c","id"]']), 0)

    def test_delegate_status_maps_to_fixed_outcomes(self):
        self.assertEqual(self.r.classify({'status': 'sent'}), ('ok', 'sent'))
        self.assertEqual(self.r.classify({'status': 'ok'}), ('ok', 'ok'))
        self.assertEqual(self.r.classify([{'status': 'confirmed'}, {'status': 'eligible'}]), ('ok', 'confirmed'))
        self.assertEqual(self.r.classify({'status': 'unknown'}), ('unknown', 'delegate_uncertain'))
        self.assertEqual(self.r.classify({'status': 'deferred'}), ('deferred', 'deferred'))
        self.assertEqual(self.r.classify({'status': 'held'}), ('unknown', 'delegate_uncertain'))
        self.assertEqual(self.r.classify({'status': 'error', 'error': 'x'}), ('failed', 'delegate_rejected'))
        self.assertEqual(self.r.classify({'status': 'not_ready'}), ('failed', 'delegate_rejected'))
        for payload in ({}, {'status': 1}, 'sent', [{'status': 'sent'}, 'x'], []):
            self.assertIsNone(self.r.classify(payload), repr(payload))

    def test_malformed_plan_json_is_usage_error_not_a_launch(self):
        self.assertEqual(self.r.main(['run', 'not json']), 1)
        self.assertEqual(self.r.main(['run']), 1)

    def test_usage_is_rejected_for_unknown_or_malformed_input(self):
        self.assertEqual(self.r.main([]), 1)
        self.assertEqual(self.r.main(['cycle', '--unknown']), 1)


if __name__ == '__main__':
    unittest.main()
