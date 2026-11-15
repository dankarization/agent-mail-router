"""Synthetic sessions.list projection: ownership never grants access."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

import agent_mail_routing as routing
import agent_mail_routing_catalog as c


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.scope = routing.RoutingScope('owner-a', 'mail-a', ('main',), (), 'p-1')
        self.base = {'key': 'agent:main:direct:1', 'sessionId': 's-1',
                     'owner': {'actor': {'type': 'human', 'id': 'owner-a'}},
                     'title': 'Orchid telescope', 'unrelatedSecret': 'NEVER_TO_MODEL'}
        self.source = routing.SelectionInput('msg-1', 'general_fallback', False, 'unbound', 'new',
                                             'Orchid telescope update', True)

    def verified(self, row, scope, agent):
        fact = routing.SessionFact(row['key'], row['sessionId'], scope.owner, scope.account, agent,
            'group' if ':group:' in row['key'] else 'private', row['title'], 'Telescope project', (),
            True, True, True, False, False, False, False, False, False, False)
        proof = c.AccessProof('scoped_acl', scope.owner, scope.account, agent, row['key'], row['sessionId'],
                              True, True, True)
        return c.ProjectedSession(fact, proof)

    def test_owned_pagination_requires_independent_acl_and_projects_whitelist(self):
        foreign = dict(self.base, key='agent:main:direct:2', title='FOREIGN_SECRET',
                       owner={'actor': {'type': 'human', 'id': 'owner-b'}})
        provider = Mock(); provider.fetch.side_effect = [
            {'sessions': [self.base], 'hasMore': True, 'nextOffset': 1, 'totalCount': 2},
            {'sessions': [foreign], 'hasMore': False, 'totalCount': 2}]
        verifier = Mock(side_effect=self.verified)
        cat = c.collect(provider, self.scope, verifier, revision='r-1', page_size=1)
        self.assertTrue(cat.complete); self.assertEqual(len(cat.candidates), 1)
        verifier.assert_called_once()
        first = provider.fetch.call_args_list[0].args[0]
        self.assertEqual(first['profileRelation'], {'profileId': 'owner-a', 'relationship': 'owned'})
        self.assertTrue(first['excludeSubagents']); self.assertTrue(first['excludeCron'])
        self.assertFalse(first['includeLastMessage']); self.assertEqual(provider.fetch.call_args_list[1].args[0]['offset'], 1)
        payload = str(routing.model_input(self.source, routing.shortlist(cat, self.source)))
        self.assertNotIn('FOREIGN_SECRET', payload); self.assertNotIn('NEVER_TO_MODEL', payload)
        self.assertNotIn(self.base['key'], payload)

    def test_owner_match_or_membership_without_access_proof_is_never_grant(self):
        provider = Mock(); provider.fetch.return_value = {'sessions': [self.base], 'hasMore': False, 'totalCount': 1}
        self.assertFalse(c.collect(provider, self.scope, lambda *_: None, revision='r').candidates)
        for bad in (lambda p: replace(p, source='membership'), lambda p: replace(p, can_deliver=False),
                    lambda p: replace(p, owner='other'), lambda p: replace(p, incarnation='stale')):
            def verify(row, scope, agent):
                result = self.verified(row, scope, agent)
                return replace(result, proof=bad(result.proof))
            self.assertFalse(c.collect(provider, self.scope, verify, revision='r').candidates)

    def test_group_needs_exact_delivery_grant_in_addition_to_scoped_acl(self):
        group = dict(self.base, key='agent:main:group:topic')
        provider = Mock(); provider.fetch.return_value = {'sessions': [group], 'hasMore': False}
        self.assertFalse(c.collect(provider, self.scope, self.verified, revision='r').candidates)
        scope = replace(self.scope, group_grants=(('main', group['key']),))
        self.assertEqual(len(c.collect(provider, scope, self.verified, revision='r').candidates), 1)

    def test_incomplete_pagination_and_fetch_failure_cannot_reach_model(self):
        pages = [{'sessions': [self.base], 'hasMore': True, 'nextOffset': 99},
                 {'sessions': [self.base], 'hasMore': True, 'nextOffset': 1, 'totalCount': 2},
                 {'sessions': [self.base], 'hasMore': False, 'totalCount': 2},
                 {'sessions': [self.base], 'hasMore': 'yes'}, {}]
        for page in pages:
            with self.subTest(page=page):
                provider = Mock(); provider.fetch.return_value = page
                cat = c.collect(provider, self.scope, self.verified, revision='r', page_size=1, max_pages=1)
                self.assertFalse(cat.complete)
                with self.assertRaises(ValueError): routing.model_input(self.source, routing.shortlist(cat, self.source))
        provider = Mock(); provider.fetch.side_effect = TimeoutError('private error')
        self.assertFalse(c.collect(provider, self.scope, self.verified, revision='r').complete)

    def test_total_page_budget_across_agents_and_unstable_count_are_incomplete(self):
        provider = Mock(); provider.fetch.return_value = {'sessions': [], 'hasMore': False, 'totalCount': 0}
        scope = replace(self.scope, allowed_agents=('main', 'other'))
        self.assertFalse(c.collect(provider, scope, self.verified, revision='r', max_pages=1).complete)
        self.assertEqual(provider.fetch.call_count, 1)
        provider.reset_mock(); provider.fetch.side_effect = [
            {'sessions':[self.base], 'hasMore': True, 'nextOffset':1, 'totalCount':2},
            {'sessions':[], 'hasMore': False, 'totalCount':1}]
        self.assertFalse(c.collect(provider, self.scope, self.verified, revision='r', page_size=1).complete)

    def test_missing_owner_wrong_agent_or_missing_incarnation_never_projected(self):
        rows = [dict(self.base, owner=None), dict(self.base, key='agent:other:private:1'),
                dict(self.base, sessionId=None), dict(self.base, owner={'actor': {'type': 'service', 'id': 'owner-a'}})]
        provider = Mock(); provider.fetch.return_value = {'sessions': rows, 'hasMore': False}
        verifier = Mock(side_effect=self.verified)
        self.assertFalse(c.collect(provider, self.scope, verifier, revision='r').candidates)
        verifier.assert_not_called()

    def test_duplicate_verified_session_fails_closed_for_whole_snapshot(self):
        provider = Mock(); provider.fetch.return_value = {'sessions': [self.base, self.base], 'hasMore': False}
        cat = c.collect(provider, self.scope, self.verified, revision='r')
        self.assertFalse(cat.complete); self.assertFalse(cat.candidates)



class ExistingGatewayTransportTests(unittest.TestCase):
    def test_provider_uses_only_read_only_sessions_list(self):
        class Gateway:
            def __init__(self): self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                return {'sessions': [], 'hasMore': False, 'totalCount': 0}
        gateway = Gateway()
        provider = c.GatewayCatalogProvider(gateway)
        params = c.page_params('owner', 'main', 0, 100)
        self.assertEqual(provider.fetch(params)['sessions'], [])
        self.assertEqual(gateway.calls, [('sessions.list', params)])
        with self.assertRaises(ValueError): c.GatewayCatalogProvider(object())

if __name__ == '__main__': unittest.main()
