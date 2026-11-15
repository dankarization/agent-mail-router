import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_mail_gateway_catalog import load_scope, collect, session_fact


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scope.json'
        self.config = {'version': 1, 'owner_profile': 'owner-profile', 'account': 'default',
                       'owner_telegram_id': '12345', 'allowed_agents': ['main'],
                       'report_group_grants': [{'agent': 'main', 'session_key': 'agent:main:telegram:group:1:topic:2'}]}
        self.path.write_text(json.dumps(self.config));self.path.chmod(0o600)
        self.cfg, self.scope = load_scope(self.path)
        self.row = {'key': 'agent:main:telegram:direct:12345:thread:12345:5', 'sessionId': 'incarnation-1',
                    'agentId': 'main', 'owner': {'actor': {'identity': {'type': 'profile', 'id': 'owner-profile'}}},
                    'sharingRole': 'owner', 'sendDisabledReason': None, 'archived': False,
                    'isBackground': False, 'isMain': False, 'isDock': False, 'classification': 'thread',
                    'status': 'done', 'kind': 'direct', 'channel': 'telegram', 'label': 'WALT funding collection',
                    'origin': {'provider': 'telegram', 'accountId': 'default', 'nativeDirectUserId': '12345'},
                    'participantCount': 2,
                    'expandedParticipants': [{'identity': {'type': 'observation', 'pluginId': 'telegram',
                        'accountId': 'default', 'id': '12345'}}, {'identity': {'type': 'agent', 'id': 'programmer'}}]}

    def tearDown(self):self.tmp.cleanup()

    def test_own_direct_and_ui(self):
        self.assertIsNotNone(session_fact(self.row, self.cfg, self.scope))
        row=copy.deepcopy(self.row);row['channel']='webchat';row['origin']={'provider':'webchat'}
        row['expandedParticipants']=[{'identity':{'type':'profile','id':'owner-profile'}}];row['participantCount']=1
        self.assertIsNotNone(session_fact(row,self.cfg,self.scope))

    def test_foreign_owner_account_person_and_participant_deny(self):
        mutations=[lambda r:r['owner']['actor']['identity'].update(id='other'),
                   lambda r:r['origin'].update(accountId='other'),
                   lambda r:r['origin'].update(nativeDirectUserId='99999'),
                   lambda r:r['expandedParticipants'].append({'identity':{'type':'profile','id':'other'}}),
                   lambda r:r['expandedParticipants'].append({'identity':{'type':'observation','pluginId':'telegram','accountId':'other','id':'12345'}}),
                   lambda r:r.update(sharingRole='participant'),
                   lambda r:r.update(sendDisabledReason='forbidden')]
        for mutate in mutations:
            row=copy.deepcopy(self.row);mutate(row)
            row['participantCount']=len(row['expandedParticipants'])
            with self.subTest(row=row):self.assertIsNone(session_fact(row,self.cfg,self.scope))

    def test_retired_background_and_unknown_facts_deny(self):
        for patch in [{'archived':True},{'isBackground':True},{'isBackground':None},
                      {'isMain':True},{'isDock':True},{'status':'paused'},
                      {'spawnedBy':'parent'},{'classification':'cron'},{'sessionId':''},
                      {'kind':'unknown'},{'origin':{}},{'expandedParticipants':None}]:
            row=copy.deepcopy(self.row);row.update(patch)
            with self.subTest(patch=patch):self.assertIsNone(session_fact(row,self.cfg,self.scope))

    def test_only_explicit_group_report_destination(self):
        row=copy.deepcopy(self.row);row.update(kind='group',key='agent:main:telegram:group:1:topic:2')
        self.assertIsNotNone(session_fact(row,self.cfg,self.scope))
        row['key']='agent:main:telegram:group:1:topic:3'
        self.assertIsNone(session_fact(row,self.cfg,self.scope))

    def test_stale_incarnation_changes_candidate_identity(self):
        def snapshot(row):return collect(self.cfg,self.scope,lambda *a:{'sessions':[row],'count':1,'totalCount':1,'hasMore':False})
        old=snapshot(self.row);row=copy.deepcopy(self.row);row['sessionId']='incarnation-2';new=snapshot(row)
        self.assertNotEqual(old.candidates[0].candidate_id,new.candidates[0].candidate_id)

    def test_complete_single_authenticated_metadata_response(self):
        requests=[];row=copy.deepcopy(self.row);row['key']+='second';row['sessionId']='second'
        def rpc(method,params):
            requests.append((method,params))
            return {'sessions':[self.row,row],'count':2,'totalCount':2,'hasMore':False}
        result=collect(self.cfg,self.scope,rpc)
        self.assertTrue(result.complete);self.assertEqual(len(result.candidates),2)
        self.assertEqual(requests[0][1]['profileRelation'],{'profileId':'owner-profile','relationship':'owned'})
        self.assertFalse(requests[0][1]['includeLastMessage']);self.assertFalse(requests[0][1]['includeDerivedTitles'])
        self.assertEqual(len(requests),1);self.assertEqual(requests[0][1]['offset'],0)

    def test_duplicate_and_partial_pages_never_become_complete(self):
        for page in [{'sessions':[self.row,self.row],'hasMore':False,'count':2,'totalCount':2},
                     {'sessions':[self.row]}, {'sessions':[],'hasMore':True,'nextOffset':1},
                     {'sessions':[self.row],'hasMore':True,'nextOffset':0},
                     {'sessions':[self.row],'hasMore':False,'count':1,'totalCount':2},
                     {'sessions':[self.row],'hasMore':False,'count':1,'totalCount':1,'offset':1},
                     {'sessions':[self.row],'hasMore':False,'count':1,'totalCount':1,'nextOffset':10}]:
            with self.subTest(page=page),self.assertRaises(ValueError):collect(self.cfg,self.scope,lambda *a:page)

    def test_public_scope_file_or_symlink_denied(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):load_scope(self.path)
        self.path.chmod(0o600);link=self.path.with_name('symlink.json');link.symlink_to(self.path)
        with self.assertRaises(ValueError):load_scope(link)

    def test_scope_generation_changes_without_reading_mail(self):
        before=self.scope.policy_revision
        self.config['aliases']={self.row['key']:['perps']};self.path.write_text(json.dumps(self.config))
        config,scope=load_scope(self.path)
        self.assertNotEqual(scope.policy_revision,before)
        self.assertEqual(session_fact(self.row,config,scope).aliases,('perps',))

    def test_real_owner_only_projection_and_incomplete_members(self):
        row=copy.deepcopy(self.row)
        row.pop('expandedParticipants');row.pop('participantCount')
        self.assertIsNotNone(session_fact(row,self.cfg,self.scope))
        row['participantCount']=1
        self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row['expandedParticipants']=[]
        self.assertIsNone(session_fact(row,self.cfg,self.scope))

    def test_missing_permission_facts_and_goal_pause_deny(self):
        for field in ['sendDisabledReason','isMain','isDock']:
            row=copy.deepcopy(self.row);row.pop(field)
            with self.subTest(field=field):self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row=copy.deepcopy(self.row);row['goal']={'status':'paused'}
        self.assertIsNone(session_fact(row,self.cfg,self.scope))
        self.cfg['paused_sessions']=[self.row['key']]
        self.assertIsNone(session_fact(self.row,self.cfg,self.scope))

    def test_historical_direct_origin_requires_exact_owner_peer(self):
        row=copy.deepcopy(self.row);row['origin'].pop('nativeDirectUserId')
        row['origin'].update(chatType='direct',**{'from':'telegram:12345','to':'telegram:12345:topic:55'})
        self.assertIsNotNone(session_fact(row,self.cfg,self.scope))
        for address in ['telegram:123450','telegram:group:12345','telegram:12345:topic:bad','telegram:99999']:
            row['origin']['to']=address
            with self.subTest(address=address):self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row['origin'].update(to='telegram:12345',nativeDirectUserId='99999')
        self.assertIsNone(session_fact(row,self.cfg,self.scope))

    def test_explicit_account_and_provider_conflicts_deny(self):
        row=copy.deepcopy(self.row);row['lastAccountId']='other'
        self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row=copy.deepcopy(self.row);row['origin']['provider']='slack'
        self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row=copy.deepcopy(self.row);row['channel']='slack'
        self.assertIsNone(session_fact(row,self.cfg,self.scope))
        row=copy.deepcopy(self.row);row['origin']['accountId']=''
        self.assertIsNone(session_fact(row,self.cfg,self.scope))


if __name__ == '__main__':unittest.main()
