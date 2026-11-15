"""Durable synthetic decisions: committed before call, no unknown replay."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import agent_mail_routing as routing
import agent_mail_routing_ledger as ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'mail.sqlite3'
        self.db = sqlite3.connect(self.path); self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)'); self.db.commit()
        self.scope = routing.RoutingScope('owner-a', 'mail-a', ('main',), (), 'policy-1')
        self.fact = routing.SessionFact('private:one', 'inc-1', 'owner-a', 'mail-a', 'main',
            'private', 'Orchid telescope', 'Telescope project', ('lens',),
            True, True, True, False, False, False, False, False, False, False)
        self.cat = routing.catalog(self.scope, (self.fact,), revision='rev-1', pages_complete=True)
        self.source = routing.SelectionInput('message-1', 'general_fallback', False,
                                              'unbound', 'new', 'Orchid telescope lens update', True)
        self.options = routing.shortlist(self.cat, self.source)
        self.raw = json.dumps({'candidateId': self.options.entries[0].candidate_id, 'confidence': 'high',
                               'alternatives': [], 'evidence': ['Orchid telescope'], 'reasonCode': 'matched'})

    def test_reserve_before_call_and_retrieve_validated_terminal_without_raw_text(self):
        claim = ledger.reserve(self.db, self.cat, self.source, self.options)
        self.assertEqual(claim.status, 'admitted'); self.assertFalse(self.db.in_transaction)
        self.assertEqual(ledger.reserve(self.db, self.cat, self.source, self.options).status, 'held')
        decision = ledger.finalize(self.db, self.cat, self.source, self.options, claim.token, self.raw)
        self.assertEqual(decision.status, 'selected'); self.assertFalse(self.db.in_transaction)
        again = ledger.reserve(self.db, self.cat, self.source, self.options)
        self.assertEqual(again.status, 'completed'); self.assertEqual(again.decision, decision)
        data = '\n'.join(row[0] for row in self.db.execute('SELECT value FROM meta'))
        self.assertNotIn(self.source.body, data); self.assertNotIn('Orchid telescope', data)
        self.assertEqual(self.db.execute('SELECT count(*) FROM meta').fetchone()[0], 2)

    def test_crash_or_unknown_remains_held_even_after_catalog_or_body_change(self):
        self.assertEqual(ledger.reserve(self.db, self.cat, self.source, self.options).status, 'admitted')
        with sqlite3.connect(self.path) as reopened:
            self.assertEqual(ledger.reserve(reopened, self.cat, self.source, self.options).status, 'held')
            changed_cat = routing.catalog(replace(self.scope, policy_revision='policy-2'), (self.fact,),
                                          revision='rev-2', pages_complete=True)
            changed_source = replace(self.source, body=self.source.body+' changed')
            self.assertEqual(ledger.reserve(reopened, changed_cat, changed_source,
                                            routing.shortlist(changed_cat, changed_source)).status, 'held')

    def test_wrong_token_and_stale_snapshot_cannot_finalize(self):
        claim = ledger.reserve(self.db, self.cat, self.source, self.options)
        with self.assertRaisesRegex(ValueError, 'reservation_invalid'):
            ledger.finalize(self.db, self.cat, self.source, self.options, '0'*64, self.raw)
        changed = replace(self.source, body=self.source.body+' changed')
        with self.assertRaises(ValueError):
            ledger.finalize(self.db, self.cat, changed, self.options, claim.token, self.raw)
        self.assertEqual(ledger.reserve(self.db, self.cat, self.source, self.options).status, 'held')

    def test_invalid_output_is_terminal_held_not_an_automatic_retry(self):
        claim = ledger.reserve(self.db, self.cat, self.source, self.options)
        outcome = ledger.finalize(self.db, self.cat, self.source, self.options, claim.token, '{')
        self.assertEqual((outcome.status, outcome.reason), ('held', 'invalid_output'))
        again = ledger.reserve(self.db, self.cat, self.source, self.options)
        self.assertEqual(again.status, 'completed'); self.assertEqual(again.decision, outcome)

    def test_report_only_and_existing_delivery_do_not_gain_work_or_replay(self):
        external = replace(self.source, intake_reason='general_external_report', source_report_only=True)
        options = routing.shortlist(self.cat, external)
        claim = ledger.reserve(self.db, self.cat, external, options)
        decision = ledger.finalize(self.db, self.cat, external, options, claim.token, self.raw)
        self.assertTrue(decision.source_report_only); self.assertFalse(hasattr(decision, 'execute'))
        unknown = replace(external, delivery_state='unknown')
        with self.assertRaises(ValueError): ledger.reserve(self.db, self.cat, unknown, routing.shortlist(self.cat, unknown))

    def test_corrupt_message_gate_cannot_release_unknown_across_revision(self):
        claim = ledger.reserve(self.db, self.cat, self.source, self.options)
        self.db.execute("UPDATE meta SET value=? WHERE key LIKE 'semantic_message_v1:%'",
                        (json.dumps({'version': 1, 'status': 'complete'}),)); self.db.commit()
        changed = routing.catalog(replace(self.scope, policy_revision='new'), (self.fact,),
                                  revision='new', pages_complete=True)
        self.assertEqual(ledger.reserve(self.db, changed, self.source,
                                       routing.shortlist(changed, self.source)).status, 'held')
        with self.assertRaises(ValueError): ledger.finalize(self.db, self.cat, self.source, self.options, claim.token, self.raw)

    def test_concurrent_admission_only_one_owner(self):
        def attempt(_):
            with sqlite3.connect(self.path, timeout=5) as db:
                return ledger.reserve(db, self.cat, self.source, self.options).status
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(results.count('admitted'), 1); self.assertEqual(results.count('held'), 3)

    def test_corrupted_or_missing_meta_never_auto_repairs_or_invokes(self):
        claim = ledger.reserve(self.db, self.cat, self.source, self.options)
        self.db.execute("UPDATE meta SET value='not json' WHERE key=?", ('semantic_decision_v1:'+claim.key,)); self.db.commit()
        with self.assertRaises(ValueError): ledger.finalize(self.db, self.cat, self.source, self.options, claim.token, self.raw)
        self.assertEqual(ledger.reserve(self.db, self.cat, self.source, self.options).status, 'held')
        other = sqlite3.connect(':memory:'); self.addCleanup(other.close)
        with self.assertRaises(sqlite3.OperationalError): ledger.reserve(other, self.cat, self.source, self.options)
        self.assertEqual(other.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])


if __name__ == '__main__': unittest.main()
