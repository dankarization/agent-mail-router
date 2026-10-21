"""Failure-oriented routing proof; all external boundaries are fakes."""
import base64
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "mail_router", Path(__file__).resolve().parents[1] / "agent_mail_router.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
ROOT = Path(__file__).resolve().parents[2]
NIGHT = datetime.fromisoformat("2026-10-18T09:59:59+04:00")
DAY = datetime.fromisoformat("2026-10-18T10:00:00+04:00")
LATE = datetime.fromisoformat("2026-10-18T23:59:59+04:00")
MIDNIGHT = datetime.fromisoformat("2026-10-19T00:00:00+04:00")


def msg(uid, body="", mid=None, reply="", refs=""):
    return {"uid": uid, "key": f"77:{uid}", "message_id": mid or f"<m{uid}@test.local>",
            "sender": m.SENDERS[0], "to": "router@example.invalid", "date": "18 Oct 2026",
            "subject": "synthetic mail", "body": body, "body_truncated": False,
            "in_reply_to": reply, "references": refs}


def snap(messages, last=None, validity="77"):
    return {"status": "ok", "uidvalidity": validity, "messages": messages,
            "next_cursor": {"uidvalidity": validity, "last_uid": last if last is not None else messages[-1]["uid"]},
            "has_more": False}


def result(rid, kind="result"):
    return {"route_id": rid, "kind": kind, "summary": "Автор сообщает о результате.",
            "details": "Доказательства описаны в письме; live-статус не проверен.",
            "next": "Владелец проверит в рамках прежних полномочий."}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mail.sqlite3"
        self.routes = m.routes_at(ROOT / "examples/routes.example.json")
        self.routes = {rid: route for rid, route in self.routes.items() if not route.get("fallback")}
        self.routes["alpha"]["message_ids"] = ["<alpha@test.local>"]
        self.routes["beta"]["message_ids"] = ["<beta@test.local>"]
        self.routes["gamma"]["message_ids"] = ["<gamma@test.local>"]
        self.db = m.Store(self.path, self.routes)
        self.db.bootstrap({"cursor": {"uidvalidity": "77", "last_uid": 0}, "custom": "retained"})
        self.gw = Mock()
        self.gw.inject.return_value = "context-receipt"
        self.gw.find.return_value = None
        self.tg = Mock()
        self.tg.send.return_value = {"status": "sent", "receipt": "telegram-42"}

    def tearDown(self):
        self.db.db.close()
        self.tmp.cleanup()

    def complete(self, messages, kinds=None):
        self.db.ingest(snap(messages))
        batch = self.db.batch()
        out = [result(t["route_id"], (kinds or {}).get(t["route_id"], "result")) for t in batch["topics"]]
        self.db.complete(batch["batch_id"], out)
        return batch, out

    def test_ids_win_over_other_topic_mentions_and_creation_session(self):
        a = msg(1, "[EXAMPLE-GAMMA]", reply="<alpha@test.local>")
        self.assertEqual(self.db.choose(a), ("alpha", "thread_identity"))
        b = msg(2, "broker", refs="<gamma@test.local>")
        self.assertEqual(self.db.choose(b), ("gamma", "thread_identity"))
        self.assertNotEqual(self.routes["alpha"]["parent"], self.routes["gamma"]["parent"])

    def test_ambiguous_unknown_generic_and_collision_are_triage(self):
        self.assertEqual(self.db.choose(msg(1, "broker deployment"))[1], "unmapped")
        self.assertEqual(self.db.choose(msg(1, refs="<alpha@test.local> <gamma@test.local>"))[1], "conflicting_thread_ids")
        self.db.ingest(snap([msg(1, "broker deployment")]))
        self.assertIsNone(self.db.batch())
        self.assertEqual(self.db.status()["triage_count"], 1)

    def test_child_links_real_and_paused_task_cannot_resume(self):
        batch, _ = self.complete([msg(1, reply="<beta@test.local>"), msg(2, reply="<alpha@test.local>")])
        self.assertTrue(next(t for t in batch["topics"] if t["route_id"] == "beta")["paused"])
        summary = json.loads(self.db.db.execute("SELECT payload FROM summaries WHERE route='alpha'").fetchone()[0])
        rendered = self.db.render(summary, "marker", telegram=True)
        self.assertIn(m.link(self.routes["alpha"]["parent"]), rendered)
        self.assertIn(m.link(self.routes["alpha"]["children"][0]), rendered)
        summary = json.loads(self.db.db.execute("SELECT payload FROM summaries WHERE route='beta'").fetchone()[0])
        self.assertIn("Письмо не возобновляет", self.db.render(summary, "marker"))

    def test_night_context_and_day_flush_without_model(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        for _ in range(3):
            self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        self.tg.send.assert_not_called()
        self.assertEqual(self.gw.inject.call_count, 2)
        self.assertIsNone(self.db.batch())
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 1)
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 1)

    def test_clock_boundaries_and_recheck_after_validation(self):
        self.assertFalse(m.day(NIGHT)); self.assertTrue(m.day(DAY))
        self.assertTrue(m.day(LATE)); self.assertFalse(m.day(MIDNIGHT))
        self.complete([msg(1, reply="<alpha@test.local>")])
        # Route contexts are preflighted before notification selection.
        for _ in range(2):
            self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        clock = Mock(side_effect=[LATE, MIDNIGHT])
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, clock)["status"], "deferred")
        self.tg.send.assert_not_called()

    def test_last_gate_in_real_adapter_has_no_http_at_night(self):
        account = {"channels": {"telegram": {"accounts": {"default": {"botToken": "123:test_token"}}}}}
        with patch.object(m.Path, "read_text", return_value=json.dumps(account)), patch.object(m.urllib.request, "build_opener") as opener:
            out = m.Telegram(lambda: MIDNIGHT).send(self.routes["alpha"], "test")
        self.assertEqual(out["status"], "deferred")
        opener.return_value.open.assert_not_called()

    def test_real_adapter_day_receipt_and_lost_ack_do_not_leak_secret(self):
        account = {"channels": {"telegram": {"accounts": {"default": {"botToken": "123:test_token"}}}}}
        response = Mock()
        response.read.return_value = json.dumps({"ok": True, "result": {
            "message_id": 42, "chat": {"id": 900000001}, "message_thread_id": 9002}}).encode()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(m.Path, "read_text", return_value=json.dumps(account)), patch.object(m.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(m.Telegram(lambda: DAY).send(self.routes["alpha"], "test")["status"], "sent")
            request = opener.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body["message_thread_id"], 9002)
            self.assertNotIn("disable_notification", body)
            opener.open.side_effect = TimeoutError("SECRET_MUST_NOT_LEAK")
            lost = m.Telegram(lambda: DAY).send(self.routes["alpha"], "test")
            self.assertEqual(lost["status"], "unknown")
            self.assertNotIn("SECRET_MUST_NOT_LEAK", json.dumps(lost))

    def test_disabled_account_cannot_be_bypassed(self):
        account = {"channels": {"telegram": {"enabled": False, "accounts": {"default": {"botToken": "123:test_token"}}}}}
        with patch.object(m.Path, "read_text", return_value=json.dumps(account)), patch.object(m.urllib.request, "build_opener") as opener:
            out = m.Telegram(lambda: DAY).send(self.routes["alpha"], "test")
        self.assertEqual(out["status"], "rejected")
        opener.assert_not_called()

    def test_ack_only_context_no_notifications(self):
        self.complete([msg(1, reply="<gamma@test.local>")], {"gamma": "ack"})
        for _ in range(3):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.tg.send.assert_not_called()
        self.assertEqual(self.gw.inject.call_count, 2)

    def test_unknown_send_restart_no_repeat_other_topic_continues(self):
        self.complete([msg(1, reply="<alpha@test.local>"), msg(2, reply="<gamma@test.local>")])
        for _ in range(4):
            self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        self.tg.send.side_effect = [{"status": "unknown", "error": "telegram_ack_unknown"},
                                   {"status": "sent", "receipt": "next-topic"}]
        with self.assertRaisesRegex(m.MailError, "telegram_ack_unknown"):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        for _ in range(6):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_count, 2)
        self.assertEqual(len(self.db.status()["held"]), 1)
        self.assertEqual(self.db.get("cursor")["last_uid"], 2)

    def test_crash_after_intent_and_positive_context_reconciliation(self):
        self.complete([msg(1, reply="<alpha@test.local>")], {"alpha": "ack"})
        with self.db.db:
            self.db.db.execute("UPDATE effects SET status='inflight' WHERE target=?", (self.routes["alpha"]["parent"],))
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.gw.find.return_value = "observed-marker-receipt"
        # Independent pending child is delivered first; unknown parent is not replayed.
        self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        self.assertEqual(self.gw.inject.call_count, 1)
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)["status"], "reconciled")
        self.assertEqual(self.gw.inject.call_count, 1)

    def test_atomic_admission_failure_and_uidvalidity_keep_cursor(self):
        bad = msg(2); bad["sender"] = "evil@example.com"
        with self.assertRaises(m.MailError):
            self.db.ingest(snap([msg(1, reply="<alpha@test.local>"), bad]))
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM mail").fetchone()[0], 0)
        self.assertEqual(self.db.get("cursor")["last_uid"], 0)
        with self.assertRaises(m.MailError):
            self.db.ingest(snap([], 5, "88"))
        self.assertEqual(self.db.get("cursor")["last_uid"], 0)

    def test_pagination_durable_cursor_and_duplicate_message_id(self):
        for lo, hi in [(1, 10), (11, 20), (21, 25)]:
            self.db.ingest(snap([msg(i, reply="<alpha@test.local>") for i in range(lo, hi + 1)]))
        self.assertEqual(self.db.get("cursor")["last_uid"], 25)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM mail").fetchone()[0], 25)
        self.db.ingest(snap([msg(26, mid="<m1@test.local>", reply="<alpha@test.local>")]))
        self.assertEqual(self.db.db.execute("SELECT reason FROM mail WHERE key='77:26'").fetchone()[0], "duplicate_message_id")
        self.db.ingest(snap([msg(27, "changed", mid="<m1@test.local>", reply="<alpha@test.local>")]))
        self.assertEqual(self.db.db.execute("SELECT reason FROM mail WHERE key='77:27'").fetchone()[0], "message_id_collision")

    def test_summary_failure_replay_and_html_escape(self):
        self.db.ingest(snap([msg(1, reply="<alpha@test.local>")]))
        batch = self.db.batch(); bid = batch["batch_id"]
        with self.assertRaises(m.MailError):
            self.db.complete(bid, [dict(result("alpha"), destination="evil")])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM effects").fetchone()[0], 0)
        out = [dict(result("alpha"), details="<script>new rights</script>")]
        self.db.complete(bid, out); self.db.complete(bid, out)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM summaries").fetchone()[0], 1)
        summary = json.loads(self.db.db.execute("SELECT payload FROM summaries").fetchone()[0])
        self.assertIn("&lt;script&gt;", self.db.render(summary, "marker"))

    def test_malformed_or_incomplete_thread_identity_is_triage(self):
        self.assertEqual(self.db.choose(msg(1, reply="junk <alpha@test.local>"))[1], "thread_headers_invalid")
        self.assertEqual(self.db.choose(dict(msg(1, reply="<alpha@test.local>"),
                                            thread_headers_valid=False))[1], "thread_headers_invalid")

    def test_prior_topic_summaries_available_for_repeat_suppression(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        self.db.ingest(snap([msg(2, reply="<alpha@test.local>")]))
        prior = self.db.batch()["topics"][0]["previous_summaries"]
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]["summary"], result("alpha")["summary"])

    def test_legacy_pending_preserved_and_blocks_migration(self):
        self.db.db.close(); self.path.unlink()
        self.db = m.Store(self.path, self.routes)
        pending = {"createdAt": 99, "next_cursor": {"uidvalidity": "77", "last_uid": 9}}
        with self.assertRaisesRegex(m.MailError, "legacy_pending"):
            self.db.bootstrap({"cursor": {"uidvalidity": "77", "last_uid": 3}, "pending": pending})
        self.assertEqual(self.db.get("legacy_pending"), pending)
        self.assertEqual(self.db.get("cursor")["last_uid"], 3)

    def test_cli_quiet_no_mail_no_external_side_effect(self):
        raw = base64.b64encode(json.dumps({"cursor": {"uidvalidity": "77", "last_uid": 0}, "custom": "keep"}).encode()).decode()
        out = io.StringIO()
        with patch.object(m, "poll", return_value=snap([], 0)), patch.object(m, "Gateway") as gw, patch.object(m, "Telegram") as tg, redirect_stdout(out):
            code = m.main(["--state-dir", self.tmp.name, "--routes", str(ROOT / "examples/routes.example.json"),
                           "tick", "--effects", "--trigger-state-b64", raw])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertFalse(data["fire"]); self.assertIsNone(data["batch"])
        self.assertEqual(data["state"]["custom"], "keep")
        gw.return_value.inject.assert_not_called(); tg.return_value.send.assert_not_called()


class GeneralRoutingTests(unittest.TestCase):
    tearDown = RoutingTests.tearDown
    complete = RoutingTests.complete

    def setUp(self):
        RoutingTests.setUp(self)
        self.general_path = Path(self.tmp.name) / "routes.general.json"
        general = {"id": "general", "title": "Почта — общие новости",
                   "parent": "agent:main:telegram:direct:900000001:thread:900000001:9001",
                   "children": [], "fallback": True, "message_ids": [], "task_refs": [],
                   "evidence": "Synthetic operator assignment; no real delivery"}
        self.general_path.write_text(json.dumps({"version": 1, "routes": [general]+list(self.routes.values())}))
        self.routes["general"] = m.routes_at(self.general_path)["general"]

    def test_operator_config_has_one_general_parent_and_no_task_or_child(self):
        general = self.routes["general"]
        self.assertEqual(general["thread_id"], "9001")
        self.assertEqual(general["parent"], "agent:main:telegram:direct:900000001:thread:900000001:9001")
        self.assertFalse(general["children"])
        self.assertFalse(general["task_refs"])
        self.assertFalse(general["message_ids"])
        cfg = {"version": 1, "routes": list(self.routes.values())}
        for changes in ({"children": self.routes["alpha"]["children"]},
                        {"message_ids": ["<general@test.local>"]},
                        {"task_refs": ["broker"]}, {"paused": True}, {"fallback": "true"}):
            with self.subTest(changes=changes):
                invalid = json.loads(json.dumps(cfg))
                next(r for r in invalid["routes"] if r["id"] == "general").update(changes)
                path = Path(self.tmp.name) / "routes.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaises(m.MailError):
                    m.routes_at(path)
        cfg["routes"].append(dict(general, id="second-general"))
        path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(m.MailError, "route_fallback_ambiguous"):
            m.routes_at(path)

    def test_general_is_not_task_identity_and_verified_task_always_wins(self):
        self.assertEqual(self.db.choose(msg(1, "broker deployment")), ("general", "general_fallback"))
        self.assertEqual(self.db.choose(msg(1, "[EXAMPLE-GAMMA]")), ("gamma", "explicit_task_reference"))
        self.assertEqual(self.db.choose(msg(1, "[EXAMPLE-GAMMA]", reply="<alpha@test.local>")),
                         ("alpha", "thread_identity"))
        self.db.ingest(snap([msg(1)]))
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM anchors WHERE id='<m1@test.local>'").fetchone()[0], 0)
        self.assertEqual(self.db.choose(msg(2, "[EXAMPLE-GAMMA]", reply="<m1@test.local>")),
                         ("gamma", "explicit_task_reference"))
        # Even an old persisted general anchor cannot shadow task identity.
        with self.db.db:
            self.db.db.execute("INSERT INTO anchors VALUES ('<oldgeneral@test.local>','general')")
        self.assertEqual(self.db.choose(msg(2, "[EXAMPLE-GAMMA]", reply="<oldgeneral@test.local>"))[0], "gamma")
        self.assertEqual(self.db.choose(msg(2, refs="<oldgeneral@test.local> <alpha@test.local>"))[0], "alpha")

    def test_conflict_invalid_spoof_collision_retired_stay_triage(self):
        cases = [
            (msg(1, refs="<alpha@test.local> <gamma@test.local>"), "conflicting_thread_ids"),
            (msg(1, "[EXAMPLE-ALPHA] [EXAMPLE-GAMMA]"), "ambiguous_task_references"),
            (dict(msg(1), message_id=""), "message_id_invalid"),
            (dict(msg(1), thread_headers_valid=False), "thread_headers_invalid"),
            (msg(1, reply="junk <unknown@test.local>"), "thread_headers_invalid"),
            (dict(msg(1), sender="spoof@example.com"), "sender_invalid"),
        ]
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.db.choose(message), (None, reason))
        with self.db.db:
            self.db.db.execute("INSERT INTO anchors VALUES ('<retired@test.local>','removed-task')")
        self.assertEqual(self.db.choose(msg(1, reply="<retired@test.local>")), (None, "route_retired"))
        self.db.ingest(snap([msg(1), msg(2, mid="<m1@test.local>"), msg(3, "changed", mid="<m1@test.local>")]))
        self.db.reroute()
        rows = self.db.db.execute("SELECT route,reason FROM mail ORDER BY rowid").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("general", "general_fallback"),
                         (None, "duplicate_message_id"), (None, "message_id_collision")])

    def test_stored_unmapped_reroutes_without_replaying_completed_or_cursor(self):
        self.routes.pop("general")
        self.complete([msg(1, reply="<alpha@test.local>")])
        self.db.ingest(snap([msg(2), msg(3, "[EXAMPLE-GAMMA] [EXAMPLE-ALPHA]"),
                             msg(4, refs="<alpha@test.local> <gamma@test.local>")]))
        self.routes["general"] = m.routes_at(self.general_path)["general"]
        before_mail = [tuple(r) for r in self.db.db.execute("SELECT key,message_id,payload,summary_id FROM mail ORDER BY rowid")]
        before_effects = [tuple(r) for r in self.db.db.execute("SELECT * FROM effects ORDER BY rowid")]
        before_meta = [tuple(r) for r in self.db.db.execute("SELECT * FROM meta ORDER BY key")]
        self.db.reroute(); self.db.reroute()
        self.assertEqual(before_mail, [tuple(r) for r in self.db.db.execute("SELECT key,message_id,payload,summary_id FROM mail ORDER BY rowid")])
        self.assertEqual(before_effects, [tuple(r) for r in self.db.db.execute("SELECT * FROM effects ORDER BY rowid")])
        self.assertEqual(before_meta, [tuple(r) for r in self.db.db.execute("SELECT * FROM meta ORDER BY key")])
        batch = self.db.batch()
        self.assertEqual([t["route_id"] for t in batch["topics"]], ["general"])
        self.assertEqual([x["uid"] for x in batch["topics"][0]["messages"]], [2])
        self.assertEqual(self.db.status()["triage_count"], 2)
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.db.reroute()
        self.assertEqual(self.db.batch(), batch)

    def test_previously_unsafe_triage_cannot_be_downgraded_to_general(self):
        self.routes.pop("general")
        self.db.ingest(snap([msg(i) for i in range(1, 6)]))
        with self.db.db:
            for i, reason in enumerate(("ambiguous_task_references", "conflicting_thread_ids", "route_retired", "message_id_collision"), 1):
                self.db.db.execute("UPDATE mail SET reason=? WHERE key=?", (reason, f"77:{i}"))
        self.routes["general"] = m.routes_at(self.general_path)["general"]
        self.db.reroute()
        self.assertEqual([r[0] for r in self.db.db.execute("SELECT route FROM mail ORDER BY rowid")], [None, None, None, None, "general"])

    def test_general_night_context_day_notify_restart_and_dedupe(self):
        self.complete([msg(1)])
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)["kind"], "context")
        self.assertEqual(self.gw.inject.call_args.args[0], self.routes["general"]["parent"])
        self.assertIn("письмо не создаёт задачу", self.gw.inject.call_args.args[1])
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)["status"], "quiet")
        self.tg.send.assert_not_called()
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_args.args[0]["thread_id"], "9001")
        self.assertEqual(self.gw.inject.call_count, 1)
        self.db.reroute()
        self.assertIsNone(self.db.batch())
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: DAY)["status"], "quiet")
        self.tg.send.assert_called_once()

    def test_general_ack_suppressed_and_prior_summary_present(self):
        self.complete([msg(1)], {"general": "ack"})
        for _ in range(3):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.tg.send.assert_not_called()
        self.gw.inject.assert_called_once()
        self.db.ingest(snap([msg(2)]))
        self.assertEqual(self.db.batch()["topics"][0]["previous_summaries"][0]["kind"], "ack")

    def test_general_unknown_notify_keeps_receipt_state_and_known_route_proceeds(self):
        self.complete([msg(1), msg(2, reply="<alpha@test.local>")])
        for _ in range(3):
            self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        # Route names affect queue ordering; transport outcome belongs to the
        # destination, not to whichever synthetic route sorts first.
        self.tg.send.side_effect = lambda route, text: (
            {"status": "unknown", "error": "telegram_ack_unknown"} if route["id"] == "general"
            else {"status": "sent", "receipt": "known-route-receipt"})
        with self.assertRaises(m.MailError):
            for _ in range(2):
                self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        for _ in range(6):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertCountEqual([c.args[0]["id"] for c in self.tg.send.call_args_list], ["general", "alpha"])
        self.assertEqual(len(self.db.status()["held"]), 1)
        self.assertEqual(self.db.status()["held"][0]["target"], self.routes["general"]["parent"])
        self.assertEqual(self.db.db.execute("SELECT receipt FROM effects WHERE kind='notify' AND status='sent'").fetchone()[0], "known-route-receipt")

    def test_supported_context_and_exact_telegram_general_thread(self):
        route = self.routes["general"]
        gw = m.Gateway()
        with patch.object(gw, "call", side_effect=[{"sessions": [{"key": route["parent"]}]},
                                                 {"ok": True, "messageId": "general-context"}]) as call:
            gw.validate(route, route["parent"])
            self.assertEqual(gw.inject(route["parent"], "context only"), "general-context")
            self.assertEqual(call.call_args.args[0], "chat.inject")
            self.assertEqual(call.call_args.args[1]["sessionKey"], route["parent"])
        account = {"channels": {"telegram": {"accounts": {"default": {"botToken": "123:test_token"}}}}}
        response = Mock()
        response.read.return_value = json.dumps({"ok": True, "result": {
            "message_id": 51, "chat": {"id": 900000001}, "message_thread_id": 9001}}).encode()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(m.Path, "read_text", return_value=json.dumps(account)), patch.object(m.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(m.Telegram(lambda: DAY).send(route, "test")["status"], "sent")
            self.assertEqual(json.loads(opener.open.call_args.args[0].data)["message_thread_id"], 9001)
            self.assertEqual(m.Telegram(lambda: MIDNIGHT).send(route, "test")["status"], "deferred")
        opener.open.assert_called_once()


class DeliveryFallbackTests(unittest.TestCase):
    setUp = GeneralRoutingTests.setUp
    tearDown = RoutingTests.tearDown
    complete = RoutingTests.complete

    def unavailable(self, *targets):
        def validate(route, target):
            if target in targets:
                raise m.MailError("route_session_unavailable")
        self.gw.validate.side_effect = validate

    def drain(self, clock=DAY, limit=15):
        for _ in range(limit):
            status = self.db.dispatch_one(self.gw, self.tg, lambda: clock)["status"]
            if status in ("quiet", "deferred", "pending"):
                break

    def snapshot_identity(self):
        return {t: [tuple(r) for r in self.db.db.execute("SELECT * FROM " + t + " ORDER BY rowid")]
                for t in ("meta", "mail", "anchors", "batches", "summaries")}

    def test_missing_parent_redirects_existing_notify_once_preserving_identity(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        before = self.snapshot_identity()
        notify = dict(self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone())
        self.unavailable(self.routes["alpha"]["parent"])
        self.drain()
        self.assertEqual(self.snapshot_identity(), before)
        self.tg.send.assert_called_once()
        route, text = self.tg.send.call_args.args
        self.assertEqual((route["chat_id"], route["thread_id"]), ("900000001", "9001"))
        self.assertIn(result("alpha")["summary"], text)
        self.assertIn(self.routes["alpha"]["title"], text)
        self.assertIn(self.routes["alpha"]["parent"], text)
        self.assertIn("маршрут можно указать здесь", text)
        row = self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone()
        self.assertEqual((row["id"], row["target"], row["receipt"]),
                         (notify["id"], route["parent"], "telegram-42"))
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.drain()
        self.tg.send.assert_called_once()

    def test_unavailable_child_detected_before_daytime_notify(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        self.unavailable(self.routes["alpha"]["children"][0])
        self.drain()
        self.assertEqual(self.tg.send.call_args.args[0]["id"], "general")
        self.gw.inject.assert_called_once()
        self.assertEqual(self.gw.inject.call_args.args[0], self.routes["alpha"]["parent"])
        self.assertIn(self.routes["alpha"]["children"][0], self.tg.send.call_args.args[1])

    def test_archived_parent_and_child_are_proven_unavailable(self):
        route = self.routes["alpha"]
        for target in [route["parent"], *route["children"]]:
            gw = m.Gateway()
            with patch.object(gw, "call", return_value={"sessions": [{"key": target, "archived": True}]}):
                with self.assertRaisesRegex(m.MailError, "route_session_unavailable"):
                    gw.validate(route, target)

    def test_incomplete_validation_and_lineage_mismatch_never_fallback(self):
        route = self.routes["alpha"]
        gw = m.Gateway()
        for response in ({}, {"sessions": [{}]}, {"sessions": [], "hasMore": True},
                         {"sessions": [], "truncationReason": "budget"}):
            with patch.object(gw, "call", return_value=response):
                with self.assertRaisesRegex(m.MailError, "route_validation_incomplete"):
                    gw.validate(route, route["parent"])
        self.complete([msg(1, reply="<alpha@test.local>")])
        for code in ("command_failed_or_timeout", "route_parent_mismatch", "route_validation_incomplete"):
            self.gw.validate.side_effect = m.MailError(code)
            with self.assertRaisesRegex(m.MailError, code):
                self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM delivery_fallbacks").fetchone()[0], 0)
        self.tg.send.assert_not_called()
        self.gw.inject.assert_not_called()

    def test_lost_fallback_ack_is_held_after_restart_not_replayed(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        self.unavailable(self.routes["alpha"]["parent"], *self.routes["alpha"]["children"])
        self.tg.send.return_value = {"status": "unknown", "error": "telegram_ack_unknown"}
        with self.assertRaisesRegex(m.MailError, "telegram_ack_unknown"):
            self.drain()
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.drain()
        self.tg.send.assert_called_once()
        held = self.db.status()["held"]
        self.assertEqual([(r["kind"], r["target"], r["status"]) for r in held],
                         [("notify", self.routes["general"]["parent"], "unknown")])

    def test_existing_sent_or_uncertain_primary_notify_never_replays_to_fallback(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        self.unavailable(self.routes["alpha"]["parent"], *self.routes["alpha"]["children"])
        original = self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone()
        for status in ("sent", "unknown", "rejected", "inflight"):
            with self.db.db:
                self.db.db.execute("UPDATE effects SET status='pending',error=NULL WHERE kind='context'")
                self.db.db.execute("UPDATE effects SET status=?,receipt='preserved-evidence' WHERE kind='notify'", (status,))
            self.drain()
            self.assertEqual(self.db.db.execute("SELECT count(*) FROM delivery_fallbacks").fetchone()[0], 0)
            row = self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone()
            self.assertEqual((row["id"], row["target"], row["status"], row["receipt"]),
                             (original["id"], original["target"], status, "preserved-evidence"))
        self.tg.send.assert_not_called()

    def test_quiet_hours_and_paused_ack_fallback_keep_task_stopped(self):
        self.complete([msg(1, reply="<beta@test.local>")], {"beta": "ack"})
        before = self.snapshot_identity()
        self.unavailable(self.routes["beta"]["parent"])
        self.drain(NIGHT)
        self.tg.send.assert_not_called()
        row = self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone()
        self.assertEqual(row["status"], "pending")
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.drain(DAY)
        self.tg.send.assert_called_once()
        text = self.tg.send.call_args.args[1]
        self.assertIn("Задача остановлена", text)
        self.assertIn("Письмо не возобновляет работу", text)
        self.assertEqual(self.snapshot_identity(), before)
        self.gw.inject.assert_not_called()

    def test_fallback_send_time_gate_after_preparation(self):
        self.complete([msg(1, reply="<beta@test.local>")])
        self.unavailable(self.routes["beta"]["parent"])
        self.db.dispatch_one(self.gw, self.tg, lambda: LATE)
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, Mock(side_effect=[LATE, MIDNIGHT]))["status"], "deferred")
        self.tg.send.assert_not_called()
        self.assertEqual(self.db.db.execute("SELECT status FROM effects WHERE kind='notify'").fetchone()[0], "pending")

    def test_unmapped_delivery_independent_of_gateway_registry(self):
        self.complete([msg(1)])
        self.unavailable(self.routes["general"]["parent"])
        self.drain()
        self.tg.send.assert_called_once()
        self.assertEqual(self.tg.send.call_args.args[0]["thread_id"], "9001")
        self.assertIn(result("general")["summary"], self.tg.send.call_args.args[1])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM anchors WHERE id='<m1@test.local>'").fetchone()[0], 0)

    def test_available_route_uses_same_destinations_and_rendering(self):
        self.complete([msg(1, reply="<alpha@test.local>")])
        summary = json.loads(self.db.db.execute("SELECT payload FROM summaries").fetchone()[0])
        notify = self.db.db.execute("SELECT id FROM effects WHERE kind='notify'").fetchone()[0]
        self.drain()
        self.assertEqual(self.tg.send.call_args.args, (self.routes["alpha"], self.db.render(summary, "MAIL-" + notify, telegram=True)))
        self.assertEqual([c.args[0] for c in self.gw.inject.call_args_list],
                         [self.routes["alpha"]["parent"], *self.routes["alpha"]["children"]])
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM delivery_fallbacks").fetchone()[0], 0)

    def test_retry_429_keeps_one_fallback_effect_and_destination(self):
        self.complete([msg(1, reply="<beta@test.local>")])
        self.unavailable(self.routes["beta"]["parent"])
        self.tg.send.side_effect = [{"status": "pending", "error": "telegram_rejected"},
                                   {"status": "sent", "receipt": "retry-ok"}]
        with self.assertRaises(m.MailError):
            self.drain()
        before = dict(self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone())
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.drain()
        row = self.db.db.execute("SELECT * FROM effects WHERE kind='notify'").fetchone()
        self.assertEqual((row["id"], row["target"], row["receipt"]), (before["id"], before["target"], "retry-ok"))
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM effects WHERE kind='notify'").fetchone()[0], 1)
        self.assertEqual(self.tg.send.call_args_list[0].args, self.tg.send.call_args_list[1].args)

    def test_unknown_triage_not_admitted_and_no_sender_trust_change(self):
        message = dict(msg(1), sender="unknown@example.com", trust_class="unknown")
        message["content_request"] = m.source_for(message)
        self.db.ingest(snap([message]))
        self.db.reroute()
        self.assertIsNone(self.db.batch())
        self.drain()
        self.tg.send.assert_not_called()
        self.gw.inject.assert_not_called()
        self.assertEqual(self.db.status()["triage"][0]["reason"], "unknown_sender_review")

    def test_fallback_long_unicode_html_is_bounded_and_paused_notice_survives(self):
        self.complete([msg(1, reply="<beta@test.local>")])
        summary = json.loads(self.db.db.execute("SELECT payload FROM summaries").fetchone()[0])
        summary["details"] = "<>&😀" * 2000
        fallback = {"unavailable_json": json.dumps([self.routes["beta"]["parent"]])}
        text = self.db.render(summary, "MAIL-proof", telegram=True, fallback=fallback)
        visible = m.html.unescape(m.re.sub(r"<[^>]*>", "", text))
        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4000)
        self.assertIn("&lt;&gt;&amp;", text)
        self.assertIn("Задача остановлена", text)
        self.assertIn(result("beta")["summary"], text)

    def test_context_lost_ack_alone_does_not_trigger_fallback(self):
        self.complete([msg(1, reply="<beta@test.local>")])
        self.gw.inject.side_effect = TimeoutError("untrusted-private-error")
        with self.assertRaisesRegex(m.MailError, "effect_ack_unknown"):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.drain()
        self.tg.send.assert_called_once()
        self.assertEqual(self.tg.send.call_args.args[0]["id"], "beta")
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM delivery_fallbacks").fetchone()[0], 0)
        self.assertEqual(self.db.status()["held"][0]["status"], "unknown")

    def test_no_operator_fallback_never_invents_destination(self):
        self.routes.pop("general")
        self.complete([msg(1, reply="<beta@test.local>")])
        self.unavailable(self.routes["beta"]["parent"])
        with self.assertRaisesRegex(m.MailError, "route_session_unavailable"):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.tg.send.assert_not_called()
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM delivery_fallbacks").fetchone()[0], 0)

    def test_dispatch_cli_has_no_poll_seen_or_error_delivery(self):
        output = io.StringIO()
        with patch.object(m, "poll") as poll, patch.object(m, "Gateway") as gw, patch.object(m, "Telegram") as tg, redirect_stdout(output):
            code = m.main(["--state-dir", self.tmp.name, "--routes", str(self.general_path), "dispatch"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "quiet")
        poll.assert_not_called()
        gw.return_value.inject.assert_not_called()
        tg.return_value.send.assert_not_called()


class VerifiedGroupRoutingTests(unittest.TestCase):
    setUp = GeneralRoutingTests.setUp
    tearDown = RoutingTests.tearDown
    complete = RoutingTests.complete
    RID = "example-group-task"
    REF = "[EXAMPLE-TASK-20300101]"
    PARENT = "agent:main:telegram:group:-1009000000001:topic:1"

    def test_only_authorized_group_topic_and_verified_entries(self):
        route = self.routes[self.RID]
        self.assertEqual((route["parent"], route["chat_id"], route["thread_id"]),
                         (self.PARENT, "-1009000000001", "1"))
        self.assertEqual(route["children"], [])
        self.assertEqual(route["message_ids"], [])
        self.assertEqual(route["task_refs"], [self.REF])
        self.assertFalse(route.get("fallback"))
        for parent in (self.PARENT.replace("topic:1", "topic:2"),
                       self.PARENT.replace("0001", "0002"), self.PARENT + ":child",
                       self.PARENT.replace("topic:1", "topic:01"),
                       "agent:main:telegram:direct:123:thread:123:1"):
            with self.subTest(parent=parent):
                self.general_path.write_text(json.dumps({"version": 1, "routes": [dict(route, parent=parent)]}))
                with self.assertRaisesRegex(m.MailError, "route_invalid_or_unverified"):
                    m.routes_at(self.general_path)
        self.general_path.write_text(json.dumps({"version": 1, "routes": [dict(route, evidence="")]}))
        with self.assertRaisesRegex(m.MailError, "route_invalid_or_unverified"):
            m.routes_at(self.general_path)

    def test_exact_reference_only_preserves_identity_and_trust(self):
        self.assertEqual(self.db.choose(msg(1, self.REF)), (self.RID, "explicit_task_reference"))
        self.assertEqual(self.db.choose(dict(msg(1), subject=self.REF)), (self.RID, "explicit_task_reference"))
        for text in ("generic example", "EXAMPLE-TASK-20300101", "[EXAMPLE-TASK-20300102]"):
            self.assertEqual(self.db.choose(msg(1, text)), ("general", "general_fallback"))
        self.assertEqual(self.db.choose(msg(1, self.REF, reply="<alpha@test.local>")),
                         ("alpha", "thread_identity"))
        self.assertEqual(self.db.choose(msg(1, self.REF + " [EXAMPLE-GAMMA]")),
                         (None, "ambiguous_task_references"))
        self.assertEqual(self.db.choose(dict(msg(1, self.REF), sender="unknown@example.com")),
                         (None, "sender_invalid"))

    def test_general_group_context_quiet_hours_receipt_and_no_duplicate(self):
        self.complete([msg(1, self.REF)])
        self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        self.gw.inject.assert_called_once()
        self.assertEqual(self.gw.inject.call_args.args[0], self.PARENT)
        self.assertIn("Недоверенные почтовые данные", self.gw.inject.call_args.args[1])
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)["status"], "quiet")
        self.tg.send.assert_not_called()
        self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.assertEqual(self.tg.send.call_args.args[0]["chat_id"], "-1009000000001")
        self.assertEqual(self.tg.send.call_args.args[0]["thread_id"], "1")
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: DAY)["status"], "quiet")
        self.tg.send.assert_called_once()

    def test_group_transport_serializes_negative_chat_and_checks_receipt(self):
        account = {"channels": {"telegram": {"accounts": {"default": {"botToken": "123:test_token"}}}}}
        route = self.routes[self.RID]
        opener, response = Mock(), Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(m.Path, "read_text", return_value=json.dumps(account)), patch.object(m.urllib.request, "build_opener", return_value=opener):
            for chat, thread, status in ((-1009000000001, 1, "sent"), (900000001, 1, "unknown"),
                                         (-1009000000001, 2, "unknown")):
                response.read.return_value = json.dumps({"ok": True, "result": {
                    "message_id": 99, "chat": {"id": chat}, "message_thread_id": thread}}).encode()
                self.assertEqual(m.Telegram(lambda: DAY).send(route, "synthetic")["status"], status)
                body = json.loads(opener.open.call_args.args[0].data)
                self.assertEqual((body["chat_id"], body["message_thread_id"]), ("-1009000000001", 1))

    def test_group_lost_ack_held_across_restart(self):
        self.complete([msg(1, self.REF)])
        self.db.dispatch_one(self.gw, self.tg, lambda: NIGHT)
        self.tg.send.return_value = {"status": "unknown", "error": "telegram_ack_unknown"}
        with self.assertRaisesRegex(m.MailError, "telegram_ack_unknown"):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        self.db.db.close(); self.db = m.Store(self.path, self.routes)
        for _ in range(3):
            self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: DAY)["status"], "quiet")
        self.tg.send.assert_called_once()
        self.assertEqual(self.db.status()["held"][0]["target"], self.PARENT)

    def test_future_route_does_not_rewrite_or_replay_delivered_general(self):
        route = self.routes.pop(self.RID)
        self.complete([msg(1, self.REF)])
        for _ in range(3):
            self.db.dispatch_one(self.gw, self.tg, lambda: DAY)
        tables = ("mail", "summaries", "effects", "meta", "anchors", "batches")
        before = {t: [tuple(r) for r in self.db.db.execute("SELECT * FROM " + t + " ORDER BY rowid")] for t in tables}
        self.routes[self.RID] = route
        self.db.reroute()
        self.assertIsNone(self.db.batch())
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg, lambda: DAY)["status"], "quiet")
        self.assertEqual(before, {t: [tuple(r) for r in self.db.db.execute("SELECT * FROM " + t + " ORDER BY rowid")] for t in tables})
        self.tg.send.assert_called_once()
        self.assertEqual(self.tg.send.call_args.args[0]["id"], "general")
        self.assertEqual(self.db.choose(msg(2, self.REF)), (self.RID, "explicit_task_reference"))


if __name__ == "__main__":
    unittest.main()
