import copy
import json
import sqlite3
import unittest

import test_agent_mail_gateway_catalog as fixtures
from agent_mail_gateway_catalog import collect
from agent_mail_gateway_delivery import deliver
from agent_mail_routing import SelectionInput, shortlist, decide


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CatalogTests();self.fixture.setUp();self.cfg=self.fixture.cfg;self.scope=self.fixture.scope
        self.row=self.fixture.row;self.scope_path=self.fixture.path;self.calls=[]
        self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');self.db.commit()
        self.cat=collect(self.cfg,self.scope,self.rpc)
        self.source=SelectionInput('test-mail-1','general_external_report',True,'unbound','new','WALT funding collection report',True)
        self.options=shortlist(self.cat,self.source)
        raw=json.dumps({'candidateId':self.options.entries[0].candidate_id,'confidence':'high','alternatives':[],
                        'evidence':['WALT funding'],'reasonCode':'matched'})
        self.decision=decide(self.source,self.options,raw);assert self.decision.status=='selected'
        self.calls=[]

    def tearDown(self):self.db.close();self.fixture.tearDown()

    def rpc(self,method,params):
        self.calls.append((method,params))
        if method=='sessions.list':return {'sessions':[self.row],'count':1,'totalCount':1,'hasMore':False}
        if method=='chat.inject':return {'messageId':'receipt-1'}
        raise AssertionError('unexpected method')

    def run_delivery(self,rpc=None,source_current=lambda:True):
        return deliver(self.db,self.scope_path,self.cat,self.source,self.options,self.decision,rpc or self.rpc,source_current)

    def test_one_passive_delivery_has_fence_and_durable_receipt(self):
        result=self.run_delivery();self.assertEqual(result['status'],'delivered');self.assertFalse(result['taskStarted'])
        writes=[p for m,p in self.calls if m=='chat.inject'];self.assertEqual(len(writes),1)
        self.assertEqual(writes[0]['sessionId'],'incarnation-1');self.assertEqual(writes[0]['sessionKey'],self.row['key'])
        self.assertNotIn('operatorSource',writes[0]);self.assertIn('не выдаёт прав',writes[0]['message'])
        self.assertEqual(self.run_delivery()['status'],'already_delivered')
        self.assertEqual(len([m for m,p in self.calls if m=='chat.inject']),1)

    def test_reset_and_permission_revocation_prevent_append(self):
        for patch in [{'sessionId':'new-incarnation'},{'sendDisabledReason':'revoked'},{'goal':{'status':'paused'}}]:
            original=copy.deepcopy(self.row);self.row.update(patch)
            result=self.run_delivery();self.assertEqual(result['status'],'held')
            self.assertFalse(any(m=='chat.inject' for m,p in self.calls));self.row=original

    def test_source_changed_before_or_after_catalog_prevents_append(self):
        self.assertEqual(self.run_delivery(source_current=lambda:False)['reason'],'scope_or_source_changed')
        states=iter([True,False])
        self.assertEqual(self.run_delivery(source_current=lambda:next(states))['reason'],'scope_or_source_changed')
        self.assertFalse(any(m=='chat.inject' for m,p in self.calls));self.assertEqual(self.db.execute('SELECT count(*) FROM meta').fetchone()[0],0)

    def test_configuration_changed_during_catalog_prevents_append(self):
        def rpc(method,params):
            result=self.rpc(method,params)
            if method=='sessions.list':
                config=json.loads(self.scope_path.read_text());config['aliases']={self.row['key']:['changed']};self.scope_path.write_text(json.dumps(config))
            return result
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'scope_or_source_changed')
        self.assertFalse(any(m=='chat.inject' for m,p in self.calls))

    def test_new_lexical_competitor_requires_reselection(self):
        def rpc(method,params):
            if method=='sessions.list':
                other=copy.deepcopy(self.row);other['sessionId']='second';other['key']+='second'
                return {'sessions':[self.row,other],'count':2,'totalCount':2,'hasMore':False}
            raise AssertionError('must not append')
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'destination_catalog_changed')

    def test_unknown_after_remote_effect_is_held_without_retry(self):
        remote=[]
        def rpc(method,params):
            if method=='chat.inject':remote.append(params);raise TimeoutError('unknown response')
            return self.rpc(method,params)
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'delivery_outcome_unknown')
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'previous_delivery_outcome')
        self.assertEqual(len(remote),1)
        saved=json.loads(self.db.execute('SELECT value FROM meta').fetchone()[0]);self.assertEqual(saved['status'],'pending')

    def test_malformed_receipt_stays_held(self):
        def rpc(method,params):return {} if method=='chat.inject' else self.rpc(method,params)
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'delivery_receipt_unknown')
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'previous_delivery_outcome')

    def test_model_selection_does_not_create_work_or_override_source_policy(self):
        self.assertTrue(self.source.source_report_only);result=self.run_delivery()
        self.assertTrue(result['sourceReportOnly']);self.assertFalse(result['taskStarted'])
        self.assertEqual({m for m,p in self.calls},{'sessions.list','chat.inject'})

    def test_caller_transaction_rejected_before_any_rpc_without_rollback(self):
        self.db.execute('INSERT INTO meta VALUES (?,?)',('caller','unfinished'))
        with self.assertRaises(ValueError):self.run_delivery()
        self.assertTrue(self.db.in_transaction);self.assertEqual(self.calls,[])
        self.assertEqual(self.db.execute('SELECT value FROM meta WHERE key=?',('caller',)).fetchone()[0],'unfinished')

    def test_source_change_while_waiting_for_write_lock_has_no_intent_or_delivery(self):
        connection=self.db;state={'eligible':True}
        class Proxy:
            def __getattr__(self,name):return getattr(connection,name)
            def execute(self,sql,*args):
                if sql=='BEGIN IMMEDIATE':state['eligible']=False
                return connection.execute(sql,*args)
        self.db=Proxy()
        self.assertEqual(self.run_delivery(source_current=lambda:state['eligible'])['reason'],'source_changed_during_admission')
        self.assertFalse(any(m=='chat.inject' for m,p in self.calls))
        self.assertEqual(connection.execute('SELECT count(*) FROM meta').fetchone()[0],0)

    def test_changed_journal_after_remote_effect_never_reports_success_or_retries(self):
        effects=[]
        def rpc(method,params):
            if method=='chat.inject':
                effects.append(params);self.db.execute('UPDATE meta SET value=?',('{"status":"held"}',));self.db.commit()
                return {'messageId':'committed-remotely'}
            return self.rpc(method,params)
        with self.assertRaises(ValueError):self.run_delivery(rpc=rpc)
        self.assertEqual(self.run_delivery(rpc=rpc)['reason'],'previous_delivery_outcome')
        self.assertEqual(len(effects),1)


if __name__=='__main__':unittest.main()
