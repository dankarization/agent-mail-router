"""Synthetic regression coverage: no real network, mailbox, Gateway or credentials."""
import signal
import json
import io
from contextlib import redirect_stdout
import subprocess
import time
import unittest
from unittest.mock import Mock, patch
import test_agent_mail_router as fixtures
m, msg, NIGHT = fixtures.m, fixtures.msg, fixtures.NIGHT

class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RoutingTests(methodName='runTest')
        self.f.setUp()
        self.old_deadline, self.old_started = m._tick_deadline, m._tick_started
    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        m._tick_deadline, m._tick_started = self.old_deadline, self.old_started
        self.f.tearDown()
    def test_poll_signal_remains_terminal(self):
        old = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(m.TickDeadline()))
        try:
            m._tick_started=time.monotonic(); m._tick_deadline=m._tick_started+2
            def stalled(*a, **kw):
                signal.setitimer(signal.ITIMER_REAL,.05); time.sleep(.2)
            with patch.object(m.subprocess,'run',side_effect=stalled):
                with self.assertRaises(m.TickDeadline): m.poll({'uidvalidity':'77','last_uid':0})
        finally: signal.signal(signal.SIGALRM,old)
    def test_after_intent_cancellation_recovers_unknown_not_replayed(self):
        self.f.complete([msg(1, reply='<alpha@test.local>')])
        self.f.gw.inject.side_effect=m.TickDeadline()
        with self.assertRaises(m.TickDeadline): self.f.db.dispatch_one(self.f.gw,self.f.tg,lambda:NIGHT)
        self.assertEqual(self.f.db.db.execute("SELECT status FROM effects WHERE kind='context'").fetchone()[0], 'inflight')
        self.f.db.db.close(); self.f.db=m.Store(self.f.path,self.f.routes)
        self.assertEqual(self.f.db.db.execute("SELECT status FROM effects WHERE kind='context'").fetchone()[0], 'unknown')
        self.f.db.db.execute("UPDATE effects SET status='rejected' WHERE status='pending'"); self.f.db.db.commit()
        self.f.gw.inject.reset_mock(); self.f.gw.inject.side_effect=None; self.f.gw.find.return_value=None
        self.assertEqual(self.f.db.dispatch_one(self.f.gw,self.f.tg,lambda:NIGHT)['status'],'quiet')
        self.f.gw.inject.assert_not_called()
    def test_before_intent_cancellation_retains_pending(self):
        self.f.complete([msg(1, reply='<alpha@test.local>')])
        self.f.gw.validate.side_effect=m.TickDeadline()
        with self.assertRaises(m.TickDeadline): self.f.db.dispatch_one(self.f.gw,self.f.tg,lambda:NIGHT)
        self.assertEqual(self.f.db.db.execute("SELECT status FROM effects WHERE kind='context'").fetchone()[0], 'pending')
        self.f.gw.inject.assert_not_called()
    def test_subprocess_timeout_at_global_cutoff_is_terminal(self):
        now=[10.0];m._tick_started=0;m._tick_deadline=16.0
        def timeout(*args,**kwargs):
            now[0]=16.01
            raise subprocess.TimeoutExpired(args[0],kwargs['timeout'])
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(m.subprocess,'run',side_effect=timeout):
            with self.assertRaises(m.TickDeadline):m.run_json(['fake'],timeout=10)
            now[0]=10
            with self.assertRaises(m.TickDeadline):m.poll({'uidvalidity':'77','last_uid':0})
    def test_below_global_deadline_operation_timeout_stays_fixed_error(self):
        now=[10.0];m._tick_started=0;m._tick_deadline=16.0
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(m.subprocess,'run',side_effect=subprocess.TimeoutExpired(['fake'],1)):
            with self.assertRaisesRegex(m.MailError,'command_failed_or_timeout'):m.run_json(['fake'],timeout=1)
    def test_late_new_snapshot_is_not_admitted_then_next_tick_admits_once(self):
        now=[0.0]; message=msg(1,reply='<alpha@test.local>')
        def slow_poll(cursor):
            now[0]=17.0
            return fixtures.snap([message])
        out=io.StringIO()
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(m,'routes_at',return_value=self.f.routes), patch.object(m,'poll',side_effect=slow_poll), patch.object(m,'record_error') as record, patch.object(m,'Telegram',return_value=self.f.tg), patch.object(m.Ledger,'dispatch_error',return_value={'status':'quiet'}), redirect_stdout(out):
            code=m.main(['--state-dir',self.f.tmp.name,'tick','--effects'])
        self.assertEqual(code,1);self.assertEqual(json.loads(out.getvalue())['error'],'runtime_deadline');record.assert_not_called()
        self.assertEqual(self.f.db.get('cursor')['last_uid'],0)
        self.assertEqual(self.f.db.db.execute('SELECT COUNT(*) FROM mail').fetchone()[0],0)
        self.f.tg.send.assert_not_called()
        now[0]=0;out=io.StringIO()
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(m,'routes_at',return_value=self.f.routes), patch.object(m,'poll',return_value=fixtures.snap([message])), redirect_stdout(out):
            code=m.main(['--state-dir',self.f.tmp.name,'tick'])
        self.assertEqual(code,0);self.assertEqual(self.f.db.get('cursor')['last_uid'],1)
        self.assertEqual(self.f.db.db.execute('SELECT COUNT(*) FROM mail').fetchone()[0],1)
        self.assertTrue(json.loads(out.getvalue())['fire'])
    def test_interrupted_ingest_rolls_back_message_and_cursor_atomically(self):
        original=self.f.db.put
        def injected(key,value):
            if key=='cursor':raise m.TickDeadline()
            return original(key,value)
        with patch.object(self.f.db,'put',side_effect=injected):
            with self.assertRaises(m.TickDeadline):self.f.db.ingest(fixtures.snap([msg(1,reply='<alpha@test.local>')]))
        self.assertEqual(self.f.db.get('cursor')['last_uid'],0)
        self.assertEqual(self.f.db.db.execute('SELECT COUNT(*) FROM mail').fetchone()[0],0)
        self.f.db.ingest(fixtures.snap([msg(1,reply='<alpha@test.local>')]))
        self.assertEqual(self.f.db.get('cursor')['last_uid'],1)
        self.assertEqual(self.f.db.db.execute('SELECT COUNT(*) FROM mail').fetchone()[0],1)
    def test_late_positive_reconciliation_does_not_write(self):
        self.f.complete([msg(1, reply='<alpha@test.local>')])
        self.f.db.db.execute("UPDATE effects SET status='rejected' WHERE status='pending'")
        self.f.db.db.execute("UPDATE effects SET status='unknown' WHERE kind='context' AND rowid=(SELECT MIN(rowid) FROM effects WHERE kind='context')")
        self.f.db.db.commit(); now=[10.0]; m._tick_started=0; m._tick_deadline=16
        def late(*a):now[0]=16.1;return 'synthetic-receipt'
        self.f.gw.find.side_effect=late
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]):
            with self.assertRaises(m.TickDeadline): self.f.db.dispatch_one(self.f.gw,self.f.tg,lambda:NIGHT)
        self.assertEqual(self.f.db.db.execute("SELECT status FROM effects WHERE status='unknown'").fetchone()[0],'unknown')
    def test_late_validation_error_does_not_write_fallback(self):
        self.f.complete([msg(1,reply='<alpha@test.local>')]);now=[10.0];m._tick_started=0;m._tick_deadline=16
        def late(*a):now[0]=16.1;raise m.MailError('route_session_unavailable')
        self.f.gw.validate.side_effect=late
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(self.f.db,'fallback_delivery') as fallback:
            with self.assertRaises(m.TickDeadline):self.f.db.dispatch_one(self.f.gw,self.f.tg,lambda:NIGHT)
            fallback.assert_not_called()
    def test_late_successful_command_is_terminal(self):
        now=[10.0];m._tick_started=0;m._tick_deadline=16
        def late(*a,**k):now[0]=16.1;return type('R',(),{'returncode':0,'stdout':b'{}'})()
        with patch.object(m.time,'monotonic',side_effect=lambda:now[0]), patch.object(m.subprocess,'run',side_effect=late):
            with self.assertRaises(m.TickDeadline):m.run_json(['fake'])
    def test_remaining_budget_prevents_new_child(self):
        m._tick_started=time.monotonic()-1;m._tick_deadline=time.monotonic()-.01
        with patch.object(m.subprocess,'run') as run:
            with self.assertRaises(m.TickDeadline): m.run_json(['fake'])
            run.assert_not_called()
if __name__=='__main__':unittest.main()

class WorkDeadlineTests(unittest.TestCase):
    def setUp(self):
        import test_agent_mail_work as work_fixtures
        self.work = work_fixtures.w
        self.f = work_fixtures.WorkTests(methodName='runTest'); self.f.setUp(); self.f.offer()
    def tearDown(self): self.f.tearDown()
    def test_validation_cancellation_skips_close_and_keeps_pending(self):
        self.f.gw.validate.side_effect=m.TickDeadline()
        with patch.object(self.work.State,'close',autospec=True) as close:
            with self.assertRaises(m.TickDeadline):self.f.dispatch()
            close.assert_not_called()
        self.f.gw.call.assert_not_called()
        self.assertEqual(self.f.task()['outbox'][0]['status'],'pending')
    def test_after_owner_intent_cancellation_recovers_unknown_without_resend(self):
        self.f.gw.call.side_effect=m.TickDeadline()
        with patch.object(self.work.State,'close',autospec=True) as close:
            with self.assertRaises(m.TickDeadline):self.f.dispatch()
            close.assert_not_called()
        self.f.gw.call.reset_mock();self.f.gw.call.side_effect=None
        self.f.dispatch()
        self.f.gw.call.assert_not_called()
        self.assertEqual(self.f.task()['outbox'][0]['status'],'unknown')
