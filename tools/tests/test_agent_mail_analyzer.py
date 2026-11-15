"""Paid admission, crash boundaries and receipt slots; never invokes a model."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import agent_mail_analyzer as a
import agent_mail_watchdog as w
import test_agent_mail_watchdog as observer


class AnalyzerTests(unittest.TestCase):
    for _name in ('setUp','tearDown','job','collect','snapshot','bad','baseline','admit'):
        locals()[_name] = getattr(observer.WatchdogTests, _name)
    def enable(self):
        self.w.enable_analysis([], self.now)
        self.baseline()

    def success(self, row):
        return {'status': 'ok', 'text': 'Проверить MAIN.', 'metadata': {'model_verified': True}}

    def test_policy_migration_preserves_reservations_and_audits_exact_open_rows(self):
        self.baseline(); self.admit(); self.admit('mail-delivery')
        before = self.w.status(); ids = [r['id'] for r in before['incidents']]
        self.w.enable_analysis(ids, self.now+400)
        self.w.enable_analysis(ids, self.now+401)
        self.assertEqual(self.w.status()['days'], before['days'])
        self.assertEqual(self.w.db.execute('SELECT count(*) FROM wd_policy_audit').fetchone()[0], 2)
        for r in self.w.db.execute('SELECT previous FROM wd_policy_audit'):
            self.assertEqual(json.loads(r[0])['decision'], 'hard_cap_unavailable')
        self.assertTrue(all(r['decision']=='analysis_ready' for r in self.w.status()['incidents']))

    def test_closed_or_other_blocked_not_replayed(self):
        self.baseline(); self.admit()
        iid=self.w.status()['incidents'][0]['id']
        self.w.db.execute('UPDATE wd_incidents SET closed=?',(self.now+400,))
        with self.assertRaises(ValueError): self.w.enable_analysis([iid], self.now+500)
        self.assertEqual(self.w.status()['consumer'], 'disabled_hard_cap_unavailable')
        self.w.enable_analysis([],self.now+500)
        self.assertIsNone(self.w.claim_analysis(self.now+600))

    def test_healthy_transient_and_repeat_no_model(self):
        self.enable(); consumer=Mock(side_effect=self.success)
        self.assertIsNone(self.w.analyze_one(consumer,self.now+1))
        self.w.observe(self.bad(),self.now+2)
        self.assertIsNone(self.w.analyze_one(consumer,self.now+100))
        self.w.observe(self.bad(),self.now+303)
        self.w.analyze_one(consumer,self.now+304)
        for n in range(4):
            self.w.observe(self.bad(str(n)),self.now+500+n)
            self.w.analyze_one(consumer,self.now+500+n)
        self.assertEqual(consumer.call_count,1)
        self.assertEqual(self.w.status()['actual_admissions'],1)

    def test_actual_daily_limit_concurrent_across_components(self):
        self.enable()
        lanes={lane:{'code':'fixed','event':'fixture'} for lane in ('scheduler','observer','mail-monitor','mail-delivery')}
        self.w.observe(self.snapshot(lanes),self.now+1)
        self.w.observe(self.snapshot(lanes),self.now+302)
        def race(_):
            with closing(w.Watchdog(self.path)) as watcher:return watcher.analyze_one(self.success,self.now+400)
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(race,range(8)))
        self.assertEqual(self.w.status()['actual_admissions'],2)
        self.assertEqual(self.w.status()['llm_calls'],2)
        self.assertEqual(sum(r['decision']=='analysis_daily_block' for r in self.w.status()['incidents']),2)

    def test_crash_after_claim_never_retries_and_counts_toward_day(self):
        self.enable();self.admit()
        self.assertIsNotNone(self.w.claim_analysis(self.now+400))
        self.w.close();self.w=w.Watchdog(self.path)
        consumer=Mock();self.w.analyze_one(consumer,self.now+500)
        consumer.assert_not_called()
        self.assertEqual(self.w.status()['actual_admissions'],1)
        self.assertEqual(self.w.status()['analysis'][0]['status'],'inflight')

    def test_failure_is_terminal_and_non_recursive(self):
        self.enable();self.admit()
        consumer=Mock(side_effect=TimeoutError('PRIVATE_EXCEPTION'))
        self.w.analyze_one(consumer,self.now+400)
        self.w.analyze_one(consumer,self.now+500)
        self.w.observe(self.collect(),self.now+600)
        self.assertEqual(consumer.call_count,1)
        self.assertEqual(len(self.w.status()['incidents']),1)
        self.assertNotIn('PRIVATE',json.dumps(self.w.status()))
        self.assertEqual(self.ledger.status()['errors'],[])

    def test_paid_day_is_tbilisi_and_never_replays_terminal(self):
        self.enable();self.admit()
        self.w.analyze_one(self.success,self.now+400)
        self.w.analyze_one(Mock(side_effect=AssertionError('RETRY')),self.now+86400)
        self.assertEqual(self.w.status()['actual_admissions'],1)

    def test_claim_transaction_crash_rolls_back_both_tables(self):
        self.enable();self.admit()
        self.w.db.execute("CREATE TRIGGER crash BEFORE UPDATE OF decision ON wd_incidents BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.Error):self.w.claim_analysis(self.now+400)
        self.assertEqual(self.w.status()['actual_admissions'],0)
        self.w.db.execute('DROP TRIGGER crash')
        self.assertIsNotNone(self.w.claim_analysis(self.now+401))

    def test_completion_injected_to_main_once_and_unknown_not_resent(self):
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram:
            gateway.return_value.inject.return_value='MAIN-RECEIPT'
            a.deliver(self.w,None,False);a.deliver(self.w,None,False)
            self.assertEqual(gateway.return_value.inject.call_count,1)
            self.assertEqual(self.w.status()['analysis'][0]['main_receipt'],'MAIN-RECEIPT')
            telegram.return_value.send.assert_not_called()

    def test_deliver_skips_hundred_completed_rows_to_reach_pending(self):
        for i in range(100):
            self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status,result,main_status,notify_status) VALUES (?,?,?,?,?,?,?,?)",
                              (f'old-{i}', 'fixture', i, i+1, 'ok', 'fixture', 'sent', 'sent'))
        self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status,result) VALUES (?,?,?,?,?,?)",
                          ('new', 'fixture', 101, 102, 'ok', 'fixture'))
        with patch('agent_mail_router.Gateway') as gateway:
            gateway.return_value.inject.return_value='fixture-receipt'
            a.deliver(self.w,None,False)
            gateway.return_value.inject.assert_called_once()
        self.assertEqual(self.w.db.execute("SELECT main_status FROM wd_analysis WHERE incident='new'").fetchone()[0], 'sent')

    def test_ready_notification_precedes_hundred_held_main_lookups(self):
        from test_agent_mail_router import DAY
        for i in range(100):
            self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status,result,main_status,notify_status) VALUES (?,?,?,?,?,?,?,?)",
                              (f'held-{i}', 'fixture', i, i+1, 'ok', 'fixture', 'unknown' if i % 2 else 'inflight', 'pending'))
        self.w.db.execute("INSERT INTO wd_analysis(incident,day,started,completed,status,result,main_status,notify_status) VALUES (?,?,?,?,?,?,?,?)",
                          ('ready', 'fixture', 101, 102, 'ok', 'fixture', 'sent', 'pending'))
        with patch('agent_mail_router.Gateway') as gateway, patch('agent_mail_router.Telegram') as telegram, patch.object(a, 'datetime') as clock:
            clock.now.return_value = DAY
            gateway.return_value.find.return_value = None
            telegram.return_value.send.return_value = {'status': 'sent', 'receipt': 'fixture'}
            a.deliver(self.w, None, True)
            telegram.return_value.send.assert_called_once()
            gateway.return_value.inject.assert_not_called()
            self.assertEqual(gateway.return_value.find.call_count, 99)
        self.assertEqual(self.w.db.execute("SELECT notify_status FROM wd_analysis WHERE incident='ready'").fetchone()[0], 'sent')
        self.assertEqual(self.w.db.execute("SELECT count(*) FROM wd_analysis WHERE main_status IN ('unknown','inflight')").fetchone()[0], 100)

    def test_telegram_pending_retries_after_rate_limit(self):
        from test_agent_mail_router import DAY
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram,patch.object(a,'datetime') as clock:
            clock.now.return_value=DAY
            gateway.return_value.inject.return_value='MAIN-RECEIPT'
            telegram.return_value.send.side_effect=[{'status':'pending'}, {'status':'sent','receipt':'fixture'}]
            a.deliver(self.w,None,True)
            self.assertEqual(self.w.status()['analysis'][0]['notify_status'],'pending')
            a.deliver(self.w,None,True)
            self.assertEqual(telegram.return_value.send.call_count,2)
            self.assertEqual(self.w.status()['analysis'][0]['notify_status'],'sent')

    def test_main_injection_failure_held_no_notification_or_retry(self):
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram:
            gateway.return_value.inject.side_effect=TimeoutError('SECRET')
            gateway.return_value.find.return_value=None
            a.deliver(self.w,None,True);a.deliver(self.w,None,True)
            self.assertEqual(gateway.return_value.inject.call_count,1)
            telegram.return_value.send.assert_not_called()
            self.assertEqual(self.w.status()['analysis'][0]['main_status'],'unknown')

    def test_config_fixed_high_no_models_fallback_no_tools_no_live_directives(self):
        profile='openai:primary@example.invalid'
        config=a.run_config({'auth':{'profiles':{profile:{'provider':'openai','mode':'oauth'}}}},profile)
        self.assertEqual(config['agents']['defaults']['model']['fallbacks'],[])
        self.assertEqual(config['agents']['defaults']['model']['primary'],a.MODEL+'@'+profile)
        self.assertEqual(config['agents']['defaults']['models'][a.MODEL]['params']['thinking'],'high')
        self.assertEqual(config['tools']['deny'],['*'])
        self.assertEqual(config['plugins']['entries']['codex']['config']['appServer']['sandbox'],'read-only')
        self.assertNotIn('channels',config)

    def test_constructor_and_one_real_dispatch_contract_using_fake_process(self):
        profile='openai:primary@example.invalid'
        base=self.root/'base.json';base.write_text(json.dumps({'auth':{'profiles':{profile:{'provider':'openai','mode':'oauth'}}}}))
        config=self.root/'analyzer.json';config.write_text(json.dumps({'profile':profile,'openclaw_config':str(base)}))
        consumer=a.Consumer(config,self.state)
        def process(argv,**kwargs):
            if argv[1:3] == ['models','status']:
                self.assertEqual(argv[argv.index('--agent')+1], 'main')
                kwargs['stdout'].write(json.dumps(self.health(profile)).encode())
                fake=Mock();fake.returncode=0;fake.communicate.return_value=(None,None)
                return fake
            self.assertEqual(argv[1:3],['agent','exec'])
            self.assertNotIn('--fallback',argv)
            self.assertEqual(argv[argv.index('--thinking')+1],'high')
            self.assertEqual(argv[argv.index('--model')+1],a.MODEL+'@'+profile)
            kwargs['stdout'].write(json.dumps({'ok':True,'provider':'openai','model':'gpt-6.1-sol','final':'Ограниченный вывод.','sessionId':'session-fixture'}).encode())
            fake=Mock();fake.returncode=0;fake.communicate.return_value=(None,None)
            return fake
        self.enable();self.admit()
        with patch.object(a.subprocess,'Popen',side_effect=process) as popen:
            self.w.analyze_one(consumer,self.now+400)
            self.w.analyze_one(consumer,self.now+500)
            self.assertEqual(popen.call_count,2)
            self.assertLessEqual(len(popen.return_value.communicate.call_args.args[0]) if popen.return_value.communicate.call_args else 0,a.MAX_CONTEXT)
        self.assertEqual(self.w.status()['analysis'][0]['status'],'ok')

    def test_abandoned_analysis_completes_unknown_without_new_admission(self):
        self.enable();self.admit();self.w.claim_analysis(self.now+400)
        self.w.expire_analysis(self.now+491)
        row=self.w.status()['analysis'][0]
        self.assertEqual(row['status'],'unknown')
        self.assertIsNotNone(row['completed'])
        self.assertEqual(self.w.status()['actual_admissions'],1)
        self.assertIsNone(self.w.claim_analysis(self.now+1000))

    def test_notification_exact_route_receipt_once_after_main_receipt(self):
        from test_agent_mail_router import DAY
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram,patch.object(a,'datetime') as clock:
            clock.now.return_value=DAY
            gateway.return_value.inject.return_value='MAIN-RECEIPT'
            telegram.return_value.send.return_value={'status':'sent','receipt':'TELEGRAM-RECEIPT'}
            a.deliver(self.w,None,True);a.deliver(self.w,None,True)
            self.assertEqual(telegram.return_value.send.call_count,1)
            self.assertEqual(telegram.return_value.send.call_args.args[0],a.MAIL_TOPIC)
            row=self.w.status()['analysis'][0]
            self.assertEqual(row['notify_status'],'sent')
            self.assertEqual(row['notify_receipt'],'TELEGRAM-RECEIPT')

    def test_lost_telegram_ack_not_resent_or_analyzed(self):
        from test_agent_mail_router import DAY
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram,patch.object(a,'datetime') as clock:
            clock.now.return_value=DAY
            gateway.return_value.inject.return_value='MAIN-RECEIPT'
            telegram.return_value.send.side_effect=TimeoutError('PRIVATE')
            a.deliver(self.w,None,True);a.deliver(self.w,None,True)
            self.assertEqual(telegram.return_value.send.call_count,1)
            self.assertEqual(self.w.status()['analysis'][0]['notify_status'],'unknown')
            self.assertEqual(self.w.status()['actual_admissions'],1)

    def test_explicit_legacy_diagnosis_may_admit_during_recovery_but_not_after_close(self):
        self.baseline();self.admit()
        self.w.observe(self.snapshot(),self.now+400)
        iid=self.w.status()['incidents'][0]['id']
        self.w.enable_analysis([iid],self.now+401)
        row=self.w.claim_analysis(self.now+402)
        self.assertEqual(row['id'],iid)
        self.assertIsNotNone(row['healthy_since'])

    def test_new_healthy_recovery_not_admitted(self):
        self.enable();self.admit()
        self.w.observe(self.snapshot(),self.now+400)
        self.assertIsNone(self.w.claim_analysis(self.now+401))

    def test_native_health_fallback_before_dispatch_only(self):
        primary='openai:primary@example.invalid';fallback='openai:secondary@example.invalid'
        health={'auth':{'unusableProfiles':[{'profileId':primary,'kind':'cooldown'}],
                        'oauth':{'profiles':[{'profileId':primary,'status':'ok'}, {'profileId':fallback,'status':'expiring'}]}}}
        self.assertEqual(a.choose_profile(primary,fallback,health),fallback)
        health['auth']['unusableProfiles']=[]
        self.assertEqual(a.choose_profile(primary,fallback,health),primary)
        health['auth']['unusableProfiles']=[{'profileId':primary},{'profileId':fallback}]
        with self.assertRaises(ValueError):a.choose_profile(primary,fallback,health)

    def test_positive_main_receipt_recovers_held_slots_without_inject(self):
        from test_agent_mail_router import DAY
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        iid=self.w.status()['analysis'][0]['incident']
        for held in ('inflight','unknown'):
            with self.subTest(held=held):
                self.w.db.execute("UPDATE wd_analysis SET main_status=?,main_receipt=NULL,notify_status='pending',notify_receipt=NULL",(held,))
                with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram,patch.object(a,'datetime') as clock:
                    clock.now.return_value=DAY
                    gateway.return_value.find.return_value='PERSISTED-MAIN-RECEIPT'
                    telegram.return_value.send.return_value={'status':'sent','receipt':'TELEGRAM-RECEIPT'}
                    a.deliver(self.w,None,True);a.deliver(self.w,None,True)
                    gateway.return_value.inject.assert_not_called()
                    gateway.return_value.find.assert_called_once_with(a.MAIL_TOPIC['parent'],'MAIL-ANALYSIS-'+iid)
                    telegram.return_value.send.assert_called_once()
                    row=self.w.status()['analysis'][0]
                    self.assertEqual(row['main_receipt'],'PERSISTED-MAIN-RECEIPT')
                    self.assertEqual(row['notify_receipt'],'TELEGRAM-RECEIPT')
                    self.assertEqual(self.w.status()['actual_admissions'],1)

    def test_missing_invalid_or_failed_main_receipt_keeps_unknown_and_never_sends(self):
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        self.w.db.execute("UPDATE wd_analysis SET main_status='unknown'")
        for receipt in (None,'',{},TimeoutError('PRIVATE')):
            with self.subTest(receipt=type(receipt).__name__):
                with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram:
                    if isinstance(receipt,Exception):gateway.return_value.find.side_effect=receipt
                    else:gateway.return_value.find.return_value=receipt
                    a.deliver(self.w,None,True);a.deliver(self.w,None,True)
                    gateway.return_value.inject.assert_not_called()
                    telegram.return_value.send.assert_not_called()
                    row=self.w.status()['analysis'][0]
                    self.assertEqual(row['main_status'],'unknown')
                    self.assertIsNone(row['main_receipt'])

    def test_reconciled_main_receipt_does_not_release_unknown_telegram_slot(self):
        self.enable();self.admit();self.w.analyze_one(self.success,self.now+400)
        self.w.db.execute("UPDATE wd_analysis SET main_status='unknown',notify_status='unknown'")
        with patch('agent_mail_router.Gateway') as gateway,patch('agent_mail_router.Telegram') as telegram:
            gateway.return_value.find.return_value='PERSISTED-MAIN-RECEIPT'
            a.deliver(self.w,None,True)
            gateway.return_value.inject.assert_not_called()
            telegram.return_value.send.assert_not_called()
            self.assertEqual(self.w.status()['analysis'][0]['notify_status'],'unknown')

    @staticmethod
    def health(profile, usable=True):
        return {'auth': {'unusableProfiles': [],
                         'oauth': {'profiles': [{'profileId': profile, 'status': 'ok'}]},
                         'runtimeAuthRoutes': [{'provider': 'openai', 'runtime': 'codex',
                                               'status': 'usable' if usable else 'unavailable'}]}}

    def fixture_consumer(self, fallback=False):
        profile = 'openai:primary@example.invalid'
        secondary = 'openai:secondary@example.invalid'
        base = self.root/'base.json'
        base.write_text(json.dumps({'auth': {'profiles': {
            p: {'provider': 'openai', 'mode': 'oauth'} for p in (profile, secondary)}}}))
        config = self.root/'analyzer.json'
        cfg = {'profile': profile, 'openclaw_config': str(base)}
        if fallback:
            cfg['fallback_profile'] = secondary
        config.write_text(json.dumps(cfg))
        return a.Consumer(config, self.state), profile, secondary

    def test_slow_preflight_shares_budget_and_single_pinned_dispatch(self):
        consumer, primary, secondary = self.fixture_consumer(True)
        self.enable(); self.admit()
        health = self.health(secondary)
        health['auth']['unusableProfiles'] = [{'profileId': primary}]
        def process(argv, **kwargs):
            if argv[1:3] == ['models', 'status']:
                result = health
            else:
                self.assertEqual(argv[argv.index('--timeout')+1], '49')
                self.assertEqual(argv[argv.index('--model')+1], a.MODEL+'@'+secondary)
                pinned = json.loads(Path(argv[argv.index('--config')+1]).read_text())
                self.assertEqual(pinned['auth']['order']['openai'], [secondary])
                result = {'ok': True, 'provider': 'openai', 'model': 'gpt-6.1-sol', 'final': 'Test'}
            kwargs['stdout'].write(json.dumps(result).encode())
            fake = Mock(returncode=0)
            return fake
        # consume start, preflight start/end (25s), budget calculation, dispatch start/end.
        with patch.object(a.time, 'monotonic', side_effect=[0, 0, 25.3, 25.3, 25.3, 30]), patch.object(a.subprocess, 'Popen', side_effect=process) as popen:
            self.w.analyze_one(consumer, self.now+400)
            self.w.analyze_one(consumer, self.now+500)
        self.assertEqual(popen.call_count, 2)
        row = self.w.status()['analysis'][0]
        meta = json.loads(row['metadata'])
        self.assertEqual(row['status'], 'ok')
        self.assertFalse(meta['thinking_verified'])
        self.assertLessEqual(meta['preflight']['elapsed_seconds'] + meta['requested_deadline'] + 10, a.CONSUMER_DEADLINE)

    def test_preflight_timeout_is_terminal_without_dispatch_or_raw_error(self):
        consumer, profile, _ = self.fixture_consumer()
        self.enable(); self.admit()
        def process(argv, **kwargs):
            kwargs['stderr'].write(b'PRIVATE credential error')
            fake = Mock(pid=123, returncode=-9)
            fake.communicate.side_effect = a.subprocess.TimeoutExpired(argv, a.PREFLIGHT_DEADLINE)
            return fake
        with patch.object(a.subprocess, 'Popen', side_effect=process) as popen, patch.object(a.os, 'killpg') as kill:
            self.w.analyze_one(consumer, self.now+400)
            self.w.analyze_one(consumer, self.now+500)
        self.assertEqual(popen.call_count, 1)
        kill.assert_called_once_with(123, a.signal.SIGKILL)
        row = self.w.status()['analysis'][0]
        meta = json.loads(row['metadata'])
        self.assertEqual((row['status'], meta['stage'], meta['error_kind']), ('timeout', 'preflight', 'process_deadline'))
        self.assertNotIn('PRIVATE', row['metadata'])
        self.assertFalse(meta['provider_dispatch_verified'])
        self.assertEqual(self.w.status()['actual_admissions'], 1)

    def test_preflight_failures_never_dispatch_or_failover_after_start(self):
        for failure in ('route', 'unavailable', 'nonzero', 'json', 'start'):
            with self.subTest(failure=failure):
                consumer, profile, _ = self.fixture_consumer()
                row = {'id': 'fixture', 'lane': 'scheduler', 'generation': 1, 'opened': 1, 'last': 2, 'repeats': 1, 'healthy_since': None}
                health = self.health(profile, usable=failure != 'route')
                if failure == 'unavailable':
                    health['auth']['unusableProfiles'] = [{'profileId': profile}]
                def process(argv, **kwargs):
                    if failure == 'start':
                        raise OSError('PRIVATE')
                    kwargs['stdout'].write(b'PRIVATE invalid output' if failure == 'json' else json.dumps(health).encode())
                    return Mock(returncode=1 if failure == 'nonzero' else 0)
                with patch.object(a.subprocess, 'Popen', side_effect=process) as popen:
                    result = consumer(row)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['metadata']['stage'], 'preflight')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_dispatch_timeout_preserves_preflight_and_never_retries_profile(self):
        consumer, profile, _ = self.fixture_consumer(True)
        row = {'id': 'fixture', 'lane': 'scheduler', 'generation': 1, 'opened': 1, 'last': 2, 'repeats': 1, 'healthy_since': None}
        def process(argv, **kwargs):
            fake = Mock(pid=123, returncode=0)
            if argv[1:3] == ['models', 'status']:
                kwargs['stdout'].write(json.dumps(self.health(profile)).encode())
            else:
                fake.returncode = -9
                fake.communicate.side_effect = a.subprocess.TimeoutExpired(argv, 70)
            return fake
        with patch.object(a.subprocess, 'Popen', side_effect=process) as popen, patch.object(a.os, 'killpg'):
            result = consumer(row)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['metadata']['stage'], 'dispatch')
        self.assertEqual(result['metadata']['profile'], profile)
        self.assertEqual(result['metadata']['preflight']['process_exit'], 0)

    def test_nonzero_or_malformed_dispatch_not_accepted_as_verified_completion(self):
        consumer, profile, _ = self.fixture_consumer()
        row = {'id': 'fixture', 'lane': 'scheduler', 'generation': 1, 'opened': 1, 'last': 2, 'repeats': 1, 'healthy_since': None}
        for malformed in (False, True):
            def process(argv, **kwargs):
                health = argv[1:3] == ['models', 'status']
                result = self.health(profile) if health else {'ok': True, 'provider': 'openai', 'model': 'gpt-6.1-sol', 'final': 'Test'}
                kwargs['stdout'].write(b'PRIVATE' if malformed and not health else json.dumps(result).encode())
                return Mock(returncode=0 if health else 1)
            with patch.object(a.subprocess, 'Popen', side_effect=process) as popen:
                result = consumer(row)
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('PRIVATE', json.dumps(result))
