"""Real durable intake/outbox with synthetic mail; every transport is a fake."""
import io
from contextlib import redirect_stdout
from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_mail_content as content
from agent_mail_lifecycle import Ledger
import agent_mail_work as w
import test_agent_mail_router as f


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.root = self.state / "content"
        self.policy = json.loads((f.ROOT / "examples/work.example.json").read_text())
        self.pp = self.state / "policy.json"
        self.pp.write_text(json.dumps(self.policy))
        self.policy_patch = patch.object(w, "POLICY", self.pp)
        self.policy_patch.start()
        # Example owners are thread keys. Production forbids that send unless
        # tools.sessions.allowThreadTargets is set; these tests fake the gateway.
        self.thread_patch = patch.object(w, "thread_handoff_allowed", return_value=True)
        self.thread_patch.start()
        self.routes = f.m.routes_at(f.ROOT / "examples/routes.example.json")
        self.owner = self.routes["alpha"]["parent"]
        self.mail_route = self.policy["blocked_notification"]["parent"]
        self.routes["general"] = dict(self.routes["alpha"], id="general", parent=self.mail_route,
                                      children=[], message_ids=[], task_refs=[], fallback=True)
        (self.state / "routes.json").write_text(json.dumps({"version": 1, "routes": list(self.routes.values())}))
        self.store = f.m.Store(self.state / "mail.sqlite3", self.routes)
        self.store.bootstrap({"cursor": {"uidvalidity": "77", "last_uid": 98}})
        self.message = self.make_message(99)
        self.store.ingest(f.snap([self.message]))
        self.batch = self.store.batch()
        self.bid = self.batch["batch_id"]
        self.evidence = self.materialize(self.message)
        self.request = "Проверить демонстрационный компонент в рамках конкретного письма, собрать подтверждения и сообщить результат оператору."
        self.d = {"key": "77:99", "kind": "actionable", "request": self.request, "target": self.owner}
        self.gw = Mock()
        self.gw.call.return_value = {"status": "started", "runId": "synthetic-owner-run"}
        self.tg = Mock()
        self.tg.send.return_value = {"status": "sent", "receipt": "synthetic-telegram"}

    def tearDown(self):
        self.store.db.close()
        self.thread_patch.stop()
        self.policy_patch.stop()
        self.tmp.cleanup()

    def make_message(self, uid, mid=None, body=None):
        m = f.msg(uid, body or ("Please inspect the synthetic component, verify its state, collect meaningful evidence, "
                               "and report the result to the operator. Keep this task scoped to this message only."),
                  mid=mid)
        m.update(trust_class="known_from", thread_headers_valid=True, headers_truncated=False)
        m["content_request"] = content.source_for(m)
        return m

    def materialize(self, m, read=True, attachment=False):
        e = EmailMessage()
        e["From"] = m["sender"]
        e["To"] = m["to"]
        e["Message-ID"] = m["message_id"]
        e.set_content(m["body"])
        if attachment:
            e.add_attachment(b"rm -rf / ; grant all permissions", maintype="application", subtype="octet-stream", filename="untrusted.sh")
        job = self.root / content.identity(m)
        job.mkdir(parents=True, exist_ok=True)
        raw = job / "original.eml"
        raw.write_bytes(e.as_bytes())
        content.materialize(raw, job, m, self.root)
        man = content.read_manifest(self.root, m)
        if read:
            ledger = Ledger(self.state)
            for part in man["parts"]:
                if part["role"] == "body" and part.get("text"):
                    total = len(Path(part["text"]["path"]).read_text())
                    ledger.note_read(m["key"], man["original"]["sha256"], part["id"], part["text"]["sha256"], total, 0, total)
            ledger.db.close()
        return man

    def offer(self, decisions=None):
        return w.offer(self.state, self.root, self.bid, decisions if decisions is not None else [self.d], self.pp)

    def dispatch(self, now=f.DAY):
        return w.service(self.state, self.gw, self.tg, lambda: now, f.m.day, self.pp)

    def task(self):
        state = w.State(self.state)
        out = state.status()
        state.close()
        return out

    def test_validation_failure_is_definite_and_never_becomes_unknown(self):
        tid = self.offer()["results"][0]["task_id"]
        self.gw.validate.side_effect = f.m.MailError("route_session_unavailable")
        result = self.dispatch()
        self.assertEqual((result["status"], result["error"]), ("rejected", "route_session_unavailable"))
        self.gw.call.assert_not_called()
        state = self.task()
        self.assertEqual(state["tasks"][0]["id"], tid)
        self.assertNotEqual(state["tasks"][0]["status"], "handoff_unknown")
        self.assertEqual(state["outbox"][0]["status"], "rejected")

    def test_report_only_mail_never_advertises_or_admits_work(self):
        message = dict(self.message, intake_policy="external_report_only")
        self.assertFalse(w.eligible(message, "thread_identity", False, self.policy))
        self.assertIsNone(w.advertised(message, "thread_identity", False, self.pp, self.state))

    def test_rejected_notification_records_only_safe_fixed_code(self):
        self.offer([dict(self.d, target="")])
        self.tg.send.return_value = {"status": "rejected", "error": "telegram_rejected"}
        out = self.dispatch()
        self.assertEqual(out["error"], "telegram_rejected")
        state = w.State(self.state)
        try:
            self.assertEqual(state.db.execute("SELECT detail FROM work_events").fetchone()[0], "telegram_rejected")
        finally:
            state.close()

    def test_natural_batch_full_reads_admit_once_owner_and_lifecycle_notifications(self):
        capability = self.batch["topics"][0]["messages"][0]["authorized_work"]
        self.assertFalse(capability["authentication_verified"])
        self.assertFalse(capability["attachments_authorized"])
        tid = self.offer()["results"][0]["task_id"]
        self.assertEqual(self.offer()["results"][0]["status"], "deduplicated")
        self.assertEqual(self.dispatch()["status"], "sent")
        self.assertEqual(self.gw.call.call_count, 1)
        method, params = self.gw.call.call_args.args
        self.assertEqual(method, "sessions.send")
        self.assertEqual(params["key"], self.owner)
        self.assertEqual(params["idempotencyKey"], "mail-work-" + tid)
        self.assertIn("No raw shell/code", params["message"])
        self.assertIn(self.request, params["message"])
        state = w.State(self.state)
        self.assertTrue(state.update(tid, self.owner, "started", "Начал проверку.")["execute"])
        self.assertFalse(state.update(tid, self.owner, "started", "Повтор.")["execute"])
        state.close()
        self.assertEqual(self.dispatch(f.NIGHT)["status"], "quiet")
        self.tg.send.assert_not_called()
        self.assertEqual(self.dispatch()["status"], "sent")
        state = w.State(self.state)
        state.update(tid, self.owner, "blocker", "Требуется конкретное дополнительное разрешение.")
        state.update(tid, self.owner, "result", "Завершено с доказательствами.")
        state.close()
        self.dispatch()
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.tg.send.call_count, 3)
        self.assertEqual(len(self.task()["tasks"]), 1)
        self.assertTrue(all(o["status"] == "sent" for o in self.task()["outbox"]))
        # Ordinary report remains a separate mandatory batch and delivery.
        self.assertIsNone(self.store.db.execute("SELECT result FROM batches").fetchone()[0])
        self.store.complete(self.bid, [f.result("general")])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM summaries").fetchone()[0], 1)

    def test_unknown_lookalike_other_alias_paused_old_uid_and_bad_source_never_admit(self):
        for delta in ({"sender": "source-a@examp1e.invalid"}, {"sender": content.SENDERS[1]},
                      {"trust_class": "unknown"}, {"key": "77:98"}, {"key": "78:99"},
                      {"headers_truncated": True}, {"thread_headers_valid": False},
                      {"content_request": {"account": "wrong@example.invalid"}}):
            m = dict(self.message, **delta)
            self.assertFalse(w.advertised(m, "general_fallback", False), delta)
        self.assertIsNone(w.advertised(self.message, "general_fallback", True))
        self.assertIsNone(w.advertised(self.message, "message_id_collision", False))
        self.assertIsNone(w.advertised(self.message, "conflicting_thread_ids", False))
        self.assertFalse((self.state / "work.sqlite3").exists())

    def test_full_read_and_hash_proof_not_preview_or_forged_auth_headers(self):
        ledger = Ledger(self.state)
        with ledger.db:
            ledger.db.execute("DELETE FROM reads")
        ledger.db.close()
        with self.assertRaisesRegex(ValueError, "full_body_reads"):
            self.offer()
        self.assertFalse((self.state / "work.sqlite3").exists())
        self.materialize(self.message)
        Path(self.evidence["original"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.offer()

    def test_attachments_do_not_supply_scope_or_body_proof(self):
        self.materialize(self.message, attachment=True)
        self.offer()
        state = w.State(self.state)
        evidence = json.loads(state.db.execute("SELECT evidence FROM work_tasks").fetchone()[0])
        state.close()
        self.assertFalse(evidence["attachments_authorized"])
        self.assertEqual(len(evidence["parts"]), 1)

    def test_same_body_different_message_id_cannot_create_second_task_after_restart(self):
        tid = self.offer()["results"][0]["task_id"]
        self.store.complete(self.bid, [f.result("general")])
        second = self.make_message(100, mid="<redelivery@elsewhere.invalid>")
        self.store.ingest(f.snap([second]))
        bid2 = self.store.batch()["batch_id"]
        self.materialize(second)
        out = w.offer(self.state, self.root, bid2, [dict(self.d, key=second["key"])], self.pp)
        self.assertEqual(out["results"][0]["task_id"], tid)
        self.assertEqual(out["results"][0]["status"], "deduplicated")
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.gw.call.call_count, 1)
        self.assertEqual(len(self.task()["tasks"]), 1)

    def test_same_message_id_changed_body_fails_closed(self):
        self.offer()
        self.store.complete(self.bid, [f.result("general")])
        second = self.make_message(100, mid=self.message["message_id"], body=self.message["body"] + " changed")
        self.store.ingest(f.snap([second]))
        self.assertEqual(self.store.db.execute("SELECT reason FROM mail WHERE key='77:100'").fetchone()[0], "message_id_collision")
        self.assertIsNone(self.store.batch())
        self.assertEqual(len(self.task()["tasks"]), 1)

    def test_thread_target_without_operator_flag_is_rejected_before_send(self):
        self.offer()
        self.thread_patch.stop()
        config = self.state / "openclaw-test.json"
        config.write_text("{}")
        try:
            with patch.dict(w.os.environ, {"OPENCLAW_CONFIG": str(config)}):
                out = self.dispatch()
        finally:
            self.thread_patch.start()
        self.assertEqual(out["error"], "work_thread_target_forbidden")
        self.assertEqual(out["status"], "rejected")
        self.gw.call.assert_not_called()
        owner = next(o for o in self.task()["outbox"] if o["kind"] == "owner")
        self.assertEqual(owner["status"], "rejected")
        self.assertIsNone(owner["receipt"])
        self.dispatch()
        self.gw.call.assert_not_called()

    def test_lost_owner_ack_and_restart_never_resend_and_owner_claim_reconciles(self):
        tid = self.offer()["results"][0]["task_id"]
        self.gw.call.side_effect = TimeoutError()
        self.assertEqual(self.dispatch()["status"], "unknown")
        self.dispatch()  # one explicit handoff-blocker notification, not another owner.
        self.dispatch()
        self.assertEqual(self.gw.call.call_count, 1)
        state = w.State(self.state)
        self.assertTrue(state.update(tid, self.owner, "started", "Existing task claimed.")["execute"])
        self.assertFalse(state.update(tid, self.owner, "started", "Again.")["execute"])
        state.close()
        owner = next(o for o in self.task()["outbox"] if o["kind"] == "owner")
        self.assertEqual(owner["status"], "sent")
        self.assertTrue(owner["receipt"].startswith("owner-claim:"))
        self.dispatch()
        self.assertEqual(self.gw.call.call_count, 1)

    def test_lost_notify_ack_never_resends_after_restart(self):
        tid = self.offer()["results"][0]["task_id"]
        self.dispatch()
        state = w.State(self.state)
        state.update(tid, self.owner, "started", "Начал.")
        state.close()
        self.tg.send.side_effect = TimeoutError()
        self.assertEqual(self.dispatch()["status"], "unknown")
        self.dispatch()
        self.assertEqual(self.tg.send.call_count, 1)
        self.assertEqual(self.task()["outbox"][-1]["status"], "unknown")

    def test_interrupted_intent_is_unknown_not_requeued(self):
        self.offer()
        state = w.State(self.state)
        with state.db:
            state.db.execute("UPDATE work_outbox SET status='inflight'")
        state.close()
        self.dispatch()
        self.gw.call.assert_not_called()
        self.assertEqual(self.task()["outbox"][0]["status"], "unknown")

    def test_owner_claim_during_rpc_does_not_get_overwritten_by_lost_ack(self):
        tid = self.offer()["results"][0]["task_id"]
        def claim_then_timeout(*args):
            state = w.State(self.state)
            state.update(tid, self.owner, "started", "Claimed before ACK.")
            state.close()
            raise TimeoutError()
        self.gw.call.side_effect = claim_then_timeout
        self.dispatch()
        self.assertEqual(self.task()["tasks"][0]["status"], "started")
        self.assertEqual(self.task()["outbox"][0]["status"], "sent")

    def test_invalid_decisions_batch_unknown_owner_and_transition_fail_closed(self):
        for decisions in ([dict(self.d, key="77:98")], [dict(self.d, request="short")],
                          [dict(self.d, command="evil")], [self.d, self.d]):
            with self.assertRaises(ValueError):
                self.offer(decisions)
        tid = self.offer()["results"][0]["task_id"]
        state = w.State(self.state)
        with self.assertRaisesRegex(ValueError, "owner"):
            state.update(tid, "agent:main:wrong", "started", "No.")
        with self.assertRaisesRegex(ValueError, "transition"):
            state.update(tid, self.owner, "result", "No start.")
        state.close()

    def test_ack_no_work_and_policy_change_never_wake_owner(self):
        self.offer([dict(self.d, kind="no_work", request="ACK only")])
        self.assertEqual(self.offer()["results"][0]["status"], "deduplicated")
        self.dispatch()
        self.gw.call.assert_not_called()
        self.assertEqual(self.task()["tasks"], [])

    def test_changed_or_disabled_policy_holds_existing_outbox(self):
        self.offer()
        self.policy["authorization_ref"] = "changed"
        self.pp.write_text(json.dumps(self.policy))
        self.assertEqual(self.dispatch()["status"], "policy_changed")
        self.gw.call.assert_not_called()
        self.policy["enabled"] = False
        self.pp.write_text(json.dumps(self.policy))
        with self.assertRaises(ValueError):
            self.dispatch()
        self.gw.call.assert_not_called()

    def test_tick_services_real_work_queue_without_polling_again_or_losing_report(self):
        self.offer()
        output = io.StringIO()
        with patch.object(f.m, "poll", return_value=f.snap([], last=99)), \
                patch.object(f.m, "routes_at", return_value=self.routes), \
                patch.object(f.m, "Gateway", return_value=self.gw), \
                patch.object(f.m, "Telegram", return_value=self.tg), \
                patch.object(f.m.Ledger, "dispatch_error", return_value={"status": "quiet"}), \
                redirect_stdout(output):
            code = f.m.main(["--state-dir", str(self.state), "tick", "--effects"])
        self.assertEqual(code, 0, output.getvalue())
        event = json.loads(output.getvalue())
        self.assertTrue(event["fire"])
        self.assertEqual(event["batch"]["batch_id"], self.bid)
        self.assertEqual(event["work_dispatch"]["status"], "sent")
        self.gw.call.assert_called_once()
        self.assertIsNone(self.store.db.execute("SELECT result FROM batches").fetchone()[0])

    def test_short_ack_body_cannot_supply_an_actionable_task(self):
        self.message["body"] = "ACK"
        self.store.db.execute("UPDATE mail SET payload=? WHERE key=?", (json.dumps(self.message), self.message["key"]))
        self.store.db.commit()
        self.materialize(self.message)
        with self.assertRaisesRegex(ValueError, "detailed_bounded_body"):
            self.offer()

    def test_unavailable_work_service_does_not_suppress_mail_batch(self):
        output = io.StringIO()
        with patch.object(f.m, "poll", return_value=f.snap([], last=99)), \
                patch.object(f.m, "routes_at", return_value=self.routes), \
                patch.object(f.m.Ledger, "dispatch_error", return_value={"status": "quiet"}), \
                patch.object(w, "service", side_effect=ValueError("synthetic fault")), \
                redirect_stdout(output):
            code = f.m.main(["--state-dir", str(self.state), "tick", "--effects"])
        event = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(event["fire"])
        self.assertEqual(event["work_dispatch"]["status"], "held")

    def test_paused_route_at_dispatch_holds_owner_and_missing_body_read_holds_no_work(self):
        ledger = Ledger(self.state)
        with ledger.db:
            ledger.db.execute("DELETE FROM reads")
        ledger.db.close()
        with self.assertRaisesRegex(ValueError, "full_body_reads"):
            self.offer([dict(self.d, kind="no_work", request="ACK")])
        self.materialize(self.message)
        self.offer()
        self.store.db.execute("UPDATE mail SET route='beta',reason='thread_identity' WHERE key=?", (self.message["key"],))
        self.store.db.commit()
        self.assertEqual(self.dispatch()["status"], "source_policy_held")
        self.gw.call.assert_not_called()

    def test_unknown_requested_target_is_durable_blocker_not_start_or_email_fallback(self):
        out = self.offer([dict(self.d, target="agent:main:telegram:group:-1009999999:topic:42")])
        self.assertEqual(out["results"][0]["status"], "route_blocked")
        self.dispatch()
        self.gw.call.assert_not_called()
        self.assertIn("НЕ начата", self.tg.send.call_args.args[1])
        self.assertEqual(self.task()["tasks"][0]["status"], "route_blocked")
        self.assertEqual(len(self.task()["tasks"]), 1)

    def test_operator_resolves_blocked_route_without_creating_new_task(self):
        tid = self.offer([dict(self.d, target="")])["results"][0]["task_id"]
        out = w.resolve_route(self.state, tid, self.owner, self.pp)
        self.assertEqual((out["status"], out["task_id"]), ("queued", tid))
        self.assertEqual(len(self.task()["tasks"]), 1)
        self.assertEqual(self.dispatch()["status"], "sent")
        self.assertEqual(self.gw.call.call_args.args[1]["idempotencyKey"], "mail-work-" + tid)
        with self.assertRaisesRegex(ValueError, "work_route_not_resolvable"):
            w.resolve_route(self.state, tid, self.owner, self.pp)
        self.assertEqual(len(self.task()["tasks"]), 1)

    def test_operator_cannot_resolve_unknown_as_new_handoff(self):
        tid = self.offer()["results"][0]["task_id"]
        self.gw.call.side_effect = TimeoutError()
        self.assertEqual(self.dispatch()["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "work_route_not_resolvable"):
            w.resolve_route(self.state, tid, self.owner, self.pp)
        self.assertEqual(self.gw.call.call_count, 1)

    def test_known_work_topic_receives_owner_start_and_result_not_mail_route(self):
        target = self.routes["alpha"]["parent"]
        tid = self.offer([dict(self.d, target=target)])["results"][0]["task_id"]
        self.dispatch()
        self.assertEqual(self.gw.call.call_args.args[1]["key"], target)
        state = w.State(self.state)
        state.update(tid, target, "started", "Working in the chosen topic.")
        state.update(tid, target, "result", "Result in the chosen topic.")
        state.close()
        self.dispatch()
        self.dispatch()
        self.assertTrue(all(call.args[0]["parent"] == target for call in self.tg.send.call_args_list))
        self.assertNotEqual(target, self.mail_route)

    def test_progress_milestone_retry_deduplicates_pings_in_work_topic(self):
        tid = self.offer()["results"][0]["task_id"]
        self.dispatch()
        state = w.State(self.state)
        state.update(tid, self.owner, "started", "Started.")
        state.update(tid, self.owner, "progress", "First check done.", "checked")
        self.assertFalse(state.update(tid, self.owner, "progress", "Retry same milestone.", "checked")["execute"])
        state.update(tid, self.owner, "progress", "Second check done.", "verified")
        state.close()
        self.dispatch()
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.tg.send.call_count, 3)
        self.assertTrue(all(call.args[0]["parent"] == self.owner for call in self.tg.send.call_args_list))

    def test_real_route_failure_keeps_task_and_concrete_blocker_without_start_claim(self):
        self.offer()
        self.gw.validate.side_effect = f.m.MailError("route_session_unavailable")
        out = self.dispatch()
        self.assertEqual(out["error"], "route_session_unavailable")
        self.gw.call.assert_not_called()
        self.dispatch()
        self.assertIn("route_session_unavailable", self.tg.send.call_args.args[1])
        self.assertEqual(len(self.task()["tasks"]), 1)
        self.assertEqual(self.task()["tasks"][0]["status"], "queued")

    def test_completed_mail_batch_does_not_destroy_durable_work_recovery(self):
        self.store.complete(self.bid, [f.result("general")])
        self.offer()
        self.dispatch()
        self.assertEqual(self.gw.call.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM effects").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
