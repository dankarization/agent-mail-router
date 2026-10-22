"""Synthetic first-writer completion, retry and conflict boundaries; no transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import fcntl
import io
import json
import sqlite3
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_agent_mail_router as fixtures
from test_agent_mail_router import m, msg, result, snap
import agent_mail_seen as seen


class CompletionTests(unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp
    tearDown = fixtures.RoutingTests.tearDown

    def admit(self, two_topics=False):
        messages = [msg(1, reply="<alpha@test.local>")]
        if two_topics:
            messages.append(msg(2, reply="<gamma@test.local>"))
        self.db.ingest(snap(messages))
        batch = self.db.batch()
        return batch["batch_id"], [result(t["route_id"], "ack") for t in batch["topics"]]

    def snapshot(self):
        return {table: [tuple(r) for r in self.db.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("meta", "mail", "anchors", "batches", "summaries", "effects", "delivery_fallbacks")}

    def assert_terminal(self, out, status):
        self.assertEqual(out["status"], status)
        self.assertIs(out["terminal"], True)
        self.assertEqual(out["next_action"], "NO_REPLY")

    def test_ack_then_rephrased_ack_after_delivery_keeps_first_and_receipt(self):
        bid, first = self.admit()
        self.assert_terminal(self.db.complete(bid, first), "stored")
        for _ in range(1 + len(self.routes["alpha"]["children"])):
            self.db.dispatch_one(self.gw, self.tg)
        before = self.snapshot()
        retry = [dict(first[0], summary="Acknowledgement rephrased.", details="Same batch, new wording.",
                      next="No new instruction.")]
        out = self.db.complete(bid, retry)
        self.assert_terminal(out, "already_completed")
        self.assertEqual(out["replay"], "text_changed")
        self.assertEqual(out["accepted_result_digest"], m.digest(first))
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.db.batch())
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg)["status"], "quiet")
        self.assertEqual(self.gw.inject.call_count, 1 + len(self.routes["alpha"]["children"]))
        self.tg.send.assert_not_called()

    def test_identical_retry_after_reopen_is_read_only(self):
        bid, first = self.admit()
        self.db.complete(bid, first)
        self.db.db.close()
        self.db = m.Store(self.path, self.routes)
        before = self.snapshot()
        out = self.db.complete(bid, first)
        self.assert_terminal(out, "already_completed")
        self.assertEqual(out["replay"], "identical")
        self.assertEqual(self.snapshot(), before)

    def test_topic_order_is_not_a_conflict(self):
        bid, first = self.admit(two_topics=True)
        self.db.complete(bid, first)
        before = self.snapshot()
        out = self.db.complete(bid, first[::-1])
        self.assertEqual(out["replay"], "identical")
        self.assertEqual(self.snapshot(), before)

    def test_changed_kind_or_topic_set_is_a_true_conflict(self):
        bid, first = self.admit(two_topics=True)
        self.db.complete(bid, first)
        before = self.snapshot()
        for retry in ([dict(first[0], kind="decision"), first[1]],
                      [dict(first[0], route_id="beta"), first[1]], first[:1]):
            with self.subTest(retry=retry), self.assertRaisesRegex(m.MailError, "batch_already_completed_differently"):
                self.db.complete(bid, retry)
            self.assertEqual(self.snapshot(), before)

    def test_malformed_retry_is_not_silently_acknowledged(self):
        bid, first = self.admit()
        self.db.complete(bid, first)
        before = self.snapshot()
        for retry in (None, [None], [dict(first[0], destination="untrusted")],
                      [dict(first[0], details="")], [dict(first[0], summary="x" * 501)],
                      [dict(first[0], kind="unknown")], first + first):
            with self.subTest(retry=retry), self.assertRaises(m.MailError):
                self.db.complete(bid, retry)
            self.assertEqual(self.snapshot(), before)

    def test_old_batch_retry_does_not_consume_new_batch(self):
        bid, first = self.admit()
        self.db.complete(bid, first)
        self.db.ingest(snap([msg(2, reply="<alpha@test.local>")]))
        new_batch = self.db.batch()
        before = self.snapshot()
        self.db.complete(bid, [dict(first[0], details="Old batch retried.")])
        self.assertEqual(self.db.batch(), new_batch)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(m.MailError, "batch_not_found"):
            self.db.complete("missing-batch", first)

    def race(self, mode):
        bid, first = self.admit()
        second = [dict(first[0], **({"summary": "Other candidate."} if mode == "text" else
                                  {"kind": "decision"} if mode == "conflict" else {}))]
        barrier = threading.Barrier(2)

        def complete(candidate):
            store = m.Store(self.path, self.routes)
            try:
                barrier.wait(timeout=5)
                try:
                    return store.complete(bid, candidate)
                except m.MailError as exc:
                    return {"error": str(exc)}
            finally:
                store.db.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(complete, candidate) for candidate in (first, second)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(o.get("status") == "stored" for o in outcomes), 1)
        accepted = json.loads(self.db.db.execute("SELECT result FROM batches WHERE id=?", (bid,)).fetchone()[0])
        self.assertIn(accepted, (first, second))
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM summaries").fetchone()[0], 1)
        kinds = [r[0] for r in self.db.db.execute("SELECT kind FROM effects ORDER BY kind")]
        self.assertEqual(kinds, ["context"] * (1 + len(self.routes["alpha"]["children"]))
                         + (["notify"] if accepted[0]["kind"] != "ack" else []))
        if mode == "conflict":
            self.assertEqual(sum(o.get("error") == "batch_already_completed_differently" for o in outcomes), 1)
        else:
            self.assertEqual(sum(o.get("status") == "already_completed" for o in outcomes), 1)
        payload = json.loads(self.db.db.execute("SELECT payload FROM summaries").fetchone()[0])
        self.assertEqual(payload["summary"], accepted[0]["summary"])

    def test_concurrent_identical_candidates(self):
        self.race("identical")

    def test_concurrent_rephrased_candidates(self):
        self.race("text")

    def test_concurrent_conflicting_candidates(self):
        self.race("conflict")

    def cli(self, bid, candidate):
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(m, "routes_at", return_value=self.routes), \
                patch.object(m, "Gateway") as gateway, patch.object(m, "Telegram") as telegram, \
                patch.object(m, "poll") as poll:
            code = m.main(["--state-dir", self.tmp.name, "complete", "--batch", bid,
                           "--result-json", json.dumps(candidate)])
        gateway.assert_not_called()
        telegram.assert_not_called()
        poll.assert_not_called()
        return code, json.loads(stream.getvalue())

    def test_cli_exact_sequence_is_terminal_without_monitor_error(self):
        bid, first = self.admit()
        with patch.object(seen, "queue_if_ready", return_value=False):
            for candidate, status in ((first, "stored"), (first, "already_completed"),
                                      ([dict(first[0], details="Rephrased acknowledgement.")], "already_completed")):
                code, out = self.cli(bid, candidate)
                self.assertEqual(code, 0)
                self.assert_terminal(out, "stored")
                self.assertEqual(out["completion_status"], status)
        ledger = m.Ledger(Path(self.tmp.name))
        try:
            self.assertEqual(ledger.status()["errors"], [])
        finally:
            ledger.close()

    def test_cli_true_conflict_still_reaches_error_outbox(self):
        bid, first = self.admit()
        self.db.complete(bid, first)
        before = self.snapshot()
        code, out = self.cli(bid, [dict(first[0], kind="blocker")])
        self.assertEqual(code, 1)
        self.assertEqual(out["error"], "batch_already_completed_differently")
        self.assertEqual(self.snapshot(), before)
        ledger = m.Ledger(Path(self.tmp.name))
        try:
            self.assertEqual(ledger.status()["errors"][0]["code"], "batch_already_completed_differently")
        finally:
            ledger.close()

    def test_cli_completion_waits_for_existing_runtime_lock(self):
        bid, first = self.admit()
        ready = threading.Event()

        def hold_lock():
            with (Path(self.tmp.name) / "runtime.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                ready.set()
                threading.Event().wait(0.15)

        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(seen, "queue_if_ready", return_value=False):
            held = worker.submit(hold_lock)
            self.assertTrue(ready.wait(timeout=5))
            code, out = self.cli(bid, first)
            held.result(timeout=5)
        self.assertEqual(code, 0)
        self.assert_terminal(out, "stored")

    def test_seen_queue_failure_does_not_invalidate_committed_completion(self):
        bid, first = self.admit()
        with patch.object(seen, "queue_if_ready", side_effect=RuntimeError("synthetic")):
            code, out = self.cli(bid, first)
        self.assertEqual(code, 0)
        self.assert_terminal(out, "stored")
        self.assertEqual(out["seen_queue"], "deferred")
        ledger = m.Ledger(Path(self.tmp.name))
        try:
            errors = ledger.status()["errors"]
            self.assertEqual([(e["component"], e["code"]) for e in errors], [("mail-seen", "seen_queue_failed")])
        finally:
            ledger.close()
        with patch.object(seen, "queue_if_ready", return_value=True) as queue:
            code, out = self.cli(bid, first)
        self.assertEqual(code, 0)
        self.assert_terminal(out, "stored")
        self.assertEqual(out["completion_status"], "already_completed")
        queue.assert_called_once()  # Resume independent idempotent Seen admission after a crash/failure.

    def test_partial_write_failure_rolls_back_entire_batch(self):
        bid, first = self.admit(two_topics=True)
        before = self.snapshot()
        with self.db.db:
            self.db.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON summaries "
                               "WHEN NEW.route='gamma' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.complete(bid, first)
        self.assertEqual(self.snapshot(), before)
        with self.db.db:
            self.db.db.execute("DROP TRIGGER fail_second")
        self.assert_terminal(self.db.complete(bid, first), "stored")
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM summaries").fetchone()[0], 2)

    def test_retry_uses_frozen_result_not_changed_route_config(self):
        bid, first = self.admit()
        self.db.complete(bid, first)
        before = self.snapshot()
        self.routes.clear()
        self.assert_terminal(self.db.complete(bid, first), "already_completed")
        self.assertEqual(self.snapshot(), before)

    def test_retry_preserves_confirmed_seen_intent_and_receipt(self):
        bid, first = self.admit()
        proof = {"key": "77:1", "source_sha256": "a" * 64, "summary_id": m.digest([bid, "alpha"])}
        with patch.object(seen, "proof_for", return_value=proof):
            self.assertEqual(self.cli(bid, first)[0], 0)
            ledger = m.Ledger(Path(self.tmp.name))
            try:
                ledger.seen_result("77:1", "confirmed", {"synthetic_receipt": "kept"})
                before = [tuple(r) for r in ledger.db.execute("SELECT * FROM seen")]
                attempts = [tuple(r) for r in ledger.db.execute("SELECT * FROM seen_attempts")]
                self.assertEqual(self.cli(bid, [dict(first[0], summary="Ignored wording.")])[0], 0)
                self.assertEqual([tuple(r) for r in ledger.db.execute("SELECT * FROM seen")], before)
                self.assertEqual([tuple(r) for r in ledger.db.execute("SELECT * FROM seen_attempts")], attempts)
            finally:
                ledger.close()
