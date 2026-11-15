"""Incident-21305af regressions: real queues/CLI, synthetic clocks and transports."""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import agent_mail_lifecycle as lifecycle
import test_agent_mail_router as routing
import test_agent_mail_watchdog as observer

m = routing.m
w = observer.w


class TriggerBudgetInvariantTests(unittest.TestCase):
    """The scheduled headless trigger gives the whole tick a hard 30s wall clock.

    Before #53971 the helper used a 27s in-process alarm while the trigger exec
    used timeoutSeconds=28, so a slow tick exceeded the 30s evaluation deadline.
    A slow evaluation was cancelled mid-flight; the outcome of individual
    batches and cursors was not established. These assertions pin the intended
    deadline ordering, which cannot prevent all host stalls.
    """

    HEADLESS_TRIGGER_WALL_CLOCK_SECONDS = 30
    # Backstop only: the alarm must win and return the helper's own JSON first.
    TRIGGER_EXEC_TIMEOUT_SECONDS = 19
    POST_CHILD_OVERHEAD_SECONDS = 9

    def test_alarm_fires_strictly_inside_trigger_wall_clock(self):
        self.assertLess(m.TICK_ALARM_SECONDS, self.HEADLESS_TRIGGER_WALL_CLOCK_SECONDS)
        # Leave room for process spawn, the native exec await, result projection
        # and Code Mode resume/teardown (~9s observed after the child exits).
        self.assertGreaterEqual(
            self.HEADLESS_TRIGGER_WALL_CLOCK_SECONDS - m.TICK_ALARM_SECONDS,
            self.POST_CHILD_OVERHEAD_SECONDS)

    def test_bounded_phases_fit_inside_the_alarm(self):
        for value in (m.TICK_POLL_TIMEOUT_SECONDS, m.TICK_DELIVERY_SECONDS,
                      m.TICK_DISPOSE_SECONDS, m.TICK_SEEN_SECONDS):
            self.assertLess(value, m.TICK_ALARM_SECONDS)
        self.assertLessEqual(m.TICK_DELIVERY_SECONDS, m.TICK_ALARM_SECONDS - 2)
        self.assertLessEqual(m.TICK_DISPOSE_SECONDS, m.TICK_ALARM_SECONDS - 2)

    def test_trigger_exec_timeout_is_a_backstop_above_the_alarm(self):
        # The helper alarm must win: the router exits with its own durable
        # fail-closed JSON before the scheduler-side exec timeout can kill it.
        # The exec timeout is only a backstop if the router itself hangs.
        self.assertGreater(self.TRIGGER_EXEC_TIMEOUT_SECONDS, m.TICK_ALARM_SECONDS)
        self.assertLess(self.TRIGGER_EXEC_TIMEOUT_SECONDS,
                        self.HEADLESS_TRIGGER_WALL_CLOCK_SECONDS)
        # Even in the backstop case the exec timeout plus the post-child overhead
        # must still settle inside the trigger wall clock.
        self.assertLessEqual(
            self.TRIGGER_EXEC_TIMEOUT_SECONDS + self.POST_CHILD_OVERHEAD_SECONDS,
            self.HEADLESS_TRIGGER_WALL_CLOCK_SECONDS)

    def test_budget_env_override_rejects_non_positive_or_non_numeric(self):
        with patch.dict(m.os.environ, {'MAIL_ROUTER_TICK_TEST': '0'}):
            with self.assertRaises(m.MailError):
                m._budget_env('MAIL_ROUTER_TICK_TEST', 24)
        with patch.dict(m.os.environ, {'MAIL_ROUTER_TICK_TEST': 'nope'}):
            with self.assertRaises(m.MailError):
                m._budget_env('MAIL_ROUTER_TICK_TEST', 24)
        with patch.dict(m.os.environ, {'MAIL_ROUTER_TICK_TEST': '12'}):
            self.assertEqual(m._budget_env('MAIL_ROUTER_TICK_TEST', 24), 12)
        with patch.dict(m.os.environ, {}, clear=False):
            m.os.environ.pop('MAIL_ROUTER_TICK_TEST', None)
            self.assertEqual(m._budget_env('MAIL_ROUTER_TICK_TEST', 24), 24)


class DeliveryProgressTests(unittest.TestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown
    complete = routing.RoutingTests.complete

    def tick(self, poll_seconds=0, context_seconds=0):
        now = [0.0]
        def poll(cursor):
            now[0] += poll_seconds
            return routing.snap([], cursor['last_uid'])
        def validate(*args):
            now[0] += context_seconds
        self.gw.validate.side_effect = validate
        output = io.StringIO()
        with patch.object(m, 'poll', side_effect=poll), \
                patch.object(m.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(m, 'routes_at', return_value=self.routes), \
                patch.object(m, 'Gateway', return_value=self.gw), \
                patch.object(m, 'Telegram', return_value=self.tg), \
                patch.object(m, 'day', return_value=True), \
                patch.object(m.Ledger, 'dispatch_error', return_value={'status': 'quiet'}), \
                redirect_stdout(output):
            code = m.main(['--state-dir', self.tmp.name, 'tick', '--effects'])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_slow_validation_respects_shared_deadline_before_intent(self):
        self.complete([routing.msg(1, reply="<beta@test.local>")])
        now = [0.0]
        calls = []
        def bounded(argv, timeout=6):
            calls.append((argv[3], timeout))
            now[0] += min(6, timeout)
            if timeout < 6:
                raise m.MailError("command_failed_or_timeout")
            return {"sessions": [{"key": self.routes["beta"]["parent"]}]}
        with patch.object(m.time, "monotonic", side_effect=lambda: now[0]), patch.object(m, "run_json", side_effect=bounded):
            out = self.db.dispatch_one(m.Gateway(11), self.tg, lambda: routing.NIGHT, deadline=11)
        self.assertEqual(out["status"], "deferred")
        self.assertEqual([x[0] for x in calls], ["sessions.list"])
        self.assertEqual(self.db.db.execute("SELECT status FROM effects ORDER BY rowid LIMIT 1").fetchone()[0], "pending")

    def test_poll_within_budget_still_services_ready_notify_and_preserves_cursor(self):
        self.complete([routing.msg(1, reply='<alpha@test.local>')])
        for _ in range(2):
            self.db.dispatch_one(self.gw, self.tg, lambda: routing.NIGHT)
        before = [tuple(r) for r in self.db.db.execute("SELECT id,receipt FROM effects WHERE status='sent'")]
        cursor = self.db.get('cursor')
        out = self.tick(poll_seconds=12)
        self.assertEqual(out['dispatch'][0], {'kind': 'notify', 'status': 'sent'})
        self.tg.send.assert_called_once()
        self.assertEqual(self.db.get('cursor'), cursor)
        for row in before:
            self.assertIn(row, [tuple(r) for r in self.db.db.execute('SELECT id,receipt FROM effects')])

    def test_poll_past_delivery_budget_but_within_tick_preserves_pending_notify(self):
        # A poll that exhausts delivery budget but fits the global budget must leave the
        # effect pending and still return existing batch/cursor (exit 0), so
        # the trigger never hits its wall clock and cancels the tick mid-flight.
        self.complete([routing.msg(1, reply='<alpha@test.local>')])
        self.db.dispatch_one(self.gw, self.tg, lambda: routing.NIGHT)
        self.tg.send.reset_mock()
        out = self.tick(poll_seconds=m.TICK_DELIVERY_SECONDS + 0.25)
        self.assertEqual(out['dispatch'], [])
        self.tg.send.assert_not_called()
        self.assertEqual(self.db.db.execute(
            "SELECT status FROM effects WHERE kind='notify'").fetchone()[0], 'pending')

    def test_seven_second_context_still_reaches_notify_in_same_tick(self):
        # beta has one context; validation occurs on both context and notify.
        self.complete([routing.msg(1, reply='<beta@test.local>')])
        out = self.tick(context_seconds=5)
        self.assertEqual(out['dispatch'][:2], [{'kind': 'context', 'status': 'sent'},
                                             {'kind': 'notify', 'status': 'sent'}])
        self.tg.send.assert_called_once()

    def test_new_contexts_do_not_starve_older_ready_notify(self):
        self.complete([routing.msg(1, reply='<beta@test.local>')])
        self.db.dispatch_one(self.gw, self.tg, lambda: routing.NIGHT)
        self.complete([routing.msg(2, reply='<alpha@test.local>')])
        out = self.db.dispatch_one(self.gw, self.tg, lambda: routing.DAY)
        self.assertEqual(out, {'kind': 'notify', 'status': 'sent'})
        self.assertEqual(self.gw.inject.call_count, 1)

    def test_own_contexts_still_precede_notify_with_new_summary_order(self):
        self.complete([routing.msg(1, reply='<alpha@test.local>')])
        for _ in range(2):
            self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: routing.DAY)['kind'], 'context')
            self.tg.send.assert_not_called()
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: routing.DAY)['kind'], 'notify')

    def test_budget_exhausted_after_validation_is_pending_not_unknown(self):
        self.complete([routing.msg(1, reply='<beta@test.local>')])
        with patch.object(m.time, 'monotonic', return_value=24.5):
            out = self.db.dispatch_one(self.gw, self.tg, lambda: routing.DAY, deadline=25)
        self.assertEqual(out['status'], 'deferred')
        self.gw.inject.assert_not_called()
        self.tg.send.assert_not_called()
        self.assertEqual(self.db.db.execute("SELECT status FROM effects WHERE kind='context'").fetchone()[0], 'pending')

    def test_real_gateway_uses_remaining_budget_for_both_rpc_and_process(self):
        gateway = m.Gateway(deadline=25)
        with patch.object(m.time, 'monotonic', return_value=19), \
                patch.object(m, 'run_json', return_value={}) as run:
            gateway.call('sessions.list', {})
            self.assertEqual(run.call_args.kwargs['timeout'], 3)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--timeout')+1], '2500')
            gateway.call('chat.inject', {})
            self.assertEqual(run.call_args.kwargs['timeout'], 6)
        with patch.object(m.time, 'monotonic', return_value=25), patch.object(m, 'run_json') as run:
            with self.assertRaisesRegex(m.MailError, 'delivery_budget_exhausted'):
                gateway.call('chat.inject', {})
            run.assert_not_called()


class EligibleObservationTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'job', 'collect', 'snapshot', 'bad', 'baseline', 'admit'):
        locals()[_name] = getattr(observer.WatchdogTests, _name)

    def pending(self, kind='notify', eid='effect', sid='summary'):
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute('INSERT INTO effects(id,kind,status,summary_id) VALUES (?,?,?,?)', (eid, kind, 'pending', sid))

    def observe_at(self, seconds):
        self.job(lastTriggerEvalAtMs=(self.now+seconds)*1000, lastRunStatus='ok', consecutiveErrors=0)
        return self.w.observe(self.collect(self.now+seconds), self.now+seconds)

    def test_pending_waits_one_900s_service_window_then_stable_300s(self):
        self.baseline(); self.pending()
        for seconds in (1, 302, 600, 900):
            self.assertEqual(self.observe_at(seconds)['incidents'], [])
        self.assertIsNone(self.observe_at(901)['incidents'][0]['decision'])
        self.assertIsNone(self.observe_at(1200)['incidents'][0]['decision'])
        self.assertEqual(self.observe_at(1201)['incidents'][0]['decision'], 'hard_cap_unavailable')

    def test_delivery_before_next_service_creates_no_incident_or_admission(self):
        self.baseline(); self.pending()
        self.observe_at(1); self.observe_at(302)
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute("UPDATE effects SET status='sent',receipt='synthetic' ")
        out = self.observe_at(901)
        self.assertEqual(out['incidents'], [])
        self.assertEqual(out['actual_admissions'], 0)

    def test_pending_age_survives_observer_reopen_without_touching_source(self):
        self.baseline(); self.pending(); self.observe_at(1); self.observe_at(601)
        self.w.close(); self.w = w.Watchdog(self.path)
        self.assertIsNone(self.observe_at(901)['incidents'][0]['decision'])
        self.assertEqual(self.observe_at(1201)['incidents'][0]['decision'], 'hard_cap_unavailable')
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            self.assertEqual(db.execute('SELECT status,receipt FROM effects').fetchone(), ('pending', None))

    def test_night_and_waiting_context_do_not_age_notify(self):
        self.baseline(); self.pending(); self.pending('context', 'ctx')
        for seconds in (1, 302, 601):
            out = self.collect(self.now+seconds)
            self.assertEqual(out['delivery_pending'], [{'id': 'ctx', 'kind': 'context'}])
            self.w.observe(out, self.now+seconds)
        with sqlite3.connect(self.state/'mail.sqlite3') as db:
            db.execute("UPDATE effects SET status='sent' WHERE kind='context'")
        self.assertEqual(self.observe_at(901)['incidents'], [])
        night = self.collect(routing.NIGHT.timestamp())
        self.assertEqual(night['delivery_pending'], [])
        self.w.observe(night, routing.NIGHT.timestamp())
        self.assertEqual(self.observe_at(902)['incidents'], [])

    def test_invalid_observation_or_long_gap_cannot_prove_overdue(self):
        self.baseline(); self.pending(); self.observe_at(1); self.observe_at(601)
        self.w.observe(self.snapshot(valid=False), self.now+602)
        self.assertEqual(self.observe_at(901)['incidents'], [])
        self.assertEqual(self.observe_at(2000)['incidents'], [])


class AnalysisProjectionTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'job', 'collect', 'snapshot', 'bad', 'baseline', 'admit'):
        locals()[_name] = getattr(observer.WatchdogTests, _name)

    def spent_day(self):
        day = w.datetime.fromtimestamp(self.now, w.TZ).date().isoformat()
        for n in range(2):
            self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status) VALUES (?,?,?,?,'unknown')",
                              (str(n), day, self.now-10, self.now-1))

    def test_daily_block_before_error_delivery_does_not_promise_analysis(self):
        self.w.enable_analysis([], self.now); self.baseline(); self.spent_day(); self.admit()
        self.assertIsNone(self.w.claim_analysis(self.now+304))
        self.w.publish(self.ledger)
        tg = Mock(); tg.send.return_value = {'status': 'sent', 'receipt': 'alert'}
        self.ledger.dispatch_error(tg, lambda: routing.DAY)
        text = tg.send.call_args.args[1]
        self.assertIn('суточный лимит admissions исчерпан', text)
        self.assertNotIn('Результат придёт', text)
        self.assertEqual(self.ledger.db.execute('SELECT decision FROM watchdog_notes').fetchone()[0], 'analysis_daily_block')
        self.assertEqual(self.w.status()['actual_admissions'], 2)

    def test_enum_texts_and_sent_history_are_immutable(self):
        self.w.enable_analysis([], self.now); self.baseline(); self.admit(); self.w.publish(self.ledger)
        for state in ('analysis_ready', 'analysis_admitted', 'analysis_daily_block', 'analysis_ok',
                      'analysis_error', 'analysis_timeout', 'analysis_unknown', 'analysis_unrecognized'):
            with self.subTest(state=state):
                self.ledger.db.execute('UPDATE watchdog_notes SET decision=?', (state,))
                self.ledger.db.execute("UPDATE errors SET status='pending'")
                tg = Mock(); tg.send.return_value = {'status': 'sent', 'receipt': 'alert'}
                self.ledger.dispatch_error(tg, lambda: routing.DAY)
                text = tg.send.call_args.args[1]
                self.assertNotIn('Результат придёт отдельно', text)
                self.assertIn('автоматическая починка ограничена', text)
                self.assertNotIn('recovery=report_only', text)
        before = [tuple(r) for r in self.ledger.db.execute('SELECT * FROM errors')]
        self.w.db.execute("UPDATE wd_incidents SET decision='analysis_daily_block'")
        self.w.publish(self.ledger)
        self.assertEqual(before, [tuple(r) for r in self.ledger.db.execute('SELECT * FROM errors')])
        self.assertEqual(self.ledger.db.execute('SELECT decision FROM watchdog_notes').fetchone()[0], 'analysis_unrecognized')

    def test_terminal_status_projects_only_to_pending_note(self):
        self.w.enable_analysis([], self.now); self.baseline(); self.admit()
        consumer = Mock(return_value={'status': 'timeout', 'text': 'synthetic'})
        self.w.analyze_one(consumer, self.now+304); self.w.publish(self.ledger)
        self.assertEqual(self.ledger.db.execute('SELECT decision FROM watchdog_notes').fetchone()[0], 'analysis_timeout')

    def test_check_analyze_cli_resolves_budget_before_alert_without_consumer_call(self):
        self.w.enable_analysis([], self.now); self.baseline(); self.spent_day(); self.admit()
        events = []
        tg = Mock()
        def send(route, text):
            events.append('alert')
            self.assertIn('суточный лимит admissions исчерпан', text)
            return {'status': 'sent', 'receipt': 'alert'}
        tg.send.side_effect = send
        consumer = Mock(side_effect=AssertionError('new model call'))
        with patch('agent_mail_analyzer.Consumer', return_value=consumer), \
                patch('agent_mail_analyzer.deliver', return_value={'status': 'quiet'}), \
                patch('agent_mail_router.Telegram', return_value=tg), \
                patch.object(lifecycle, 'datetime') as dt, patch.object(w.time, 'time', return_value=self.now+304), \
                redirect_stdout(io.StringIO()):
            dt.now.return_value = routing.DAY
            code = w.main(['check-analyze', '--state-dir', str(self.state), '--scheduler-db', str(self.scheduler_path),
                           '--job-id', 'job', '--store-key', 'store', '--analyzer-config', 'unused', '--dispatch'])
        self.assertEqual(code, 0)
        self.assertEqual(events, ['alert'])
        consumer.assert_not_called()
        self.assertEqual(self.w.status()['actual_admissions'], 2)
