"""Synthetic origin lane and SMTP fault tests; no network or real mail."""
import json
import unittest
from unittest.mock import Mock
import agent_mail_origin as origin
import agent_mail_send_transport as transport
import test_agent_mail_router as base
from test_agent_mail_router import msg, snap

BINDING = {"sessionKey": "agent:main:main", "sessionId": "synthetic-generation",
           "agentId": "main", "senderId": "synthetic-owner", "channel": "telegram", "accountId": ""}
MID = "<synthetic-outbound@example.invalid>"


class OriginTests(unittest.TestCase):
    def setUp(self):
        base.RoutingTests.setUp(self)
        self.db.db.execute("""CREATE TABLE mail_outbound_v1(
            op TEXT PRIMARY KEY,mid TEXT UNIQUE,payload TEXT,origin TEXT,approval TEXT,
            created INTEGER,status TEXT,result TEXT)""")
        self.db.db.execute("INSERT INTO mail_outbound_v1 VALUES(?,?,?,?,?,?,?,NULL)",
            ("synthetic-operation", MID, "{}", json.dumps(BINDING), "confirmation", 0, "inflight"))
        self.db.db.commit()

    def tearDown(self):
        base.RoutingTests.tearDown(self)

    def test_exact_passive_delivery_and_duplicate(self):
        self.db.ingest(snap([msg(1, reply=MID)]))
        self.assertIsNone(self.db.batch())
        self.gw.call.return_value = {"ok": True, "messageId": "synthetic-injection"}
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg)["status"], "sent")
        method, args = self.gw.call.call_args.args
        self.assertEqual(method, "chat.inject")
        self.assertEqual(args["sessionKey"], BINDING["sessionKey"])
        self.assertEqual(args["sessionId"], BINDING["sessionId"])
        self.assertIn("report-only", args["message"])
        self.tg.send.assert_not_called()
        self.db.ingest(snap([msg(2, mid="<m1@test.local>", reply=MID)]))
        self.assertEqual(self.db.db.execute("SELECT reason FROM mail WHERE key='77:2'").fetchone()[0], "duplicate_message_id")
        self.db.dispatch_one(self.gw, self.tg)
        self.assertEqual(self.gw.call.call_count, 1)

    def test_timeout_reset_unknown_never_retry_or_fallback(self):
        self.db.ingest(snap([msg(1, reply=MID)]))
        self.gw.call.side_effect = TimeoutError()
        self.assertEqual(self.db.dispatch_one(self.gw, self.tg)["status"], "unknown")
        self.db.dispatch_one(self.gw, self.tg)
        self.assertEqual(self.gw.call.call_count, 1)
        self.tg.send.assert_not_called()

    def test_missing_forged_collision_and_conflicting_identity(self):
        self.assertEqual(self.db.choose(msg(1, reply="<forged@example.invalid>"))[1], "unmapped")
        self.assertEqual(self.db.choose(msg(1, mid=MID))[1], "outgoing_message_id_collision")
        self.assertEqual(self.db.choose(msg(1, reply=MID, refs="<alpha@test.local>"))[1], "conflicting_thread_ids")
        other = dict(BINDING, sessionId="different-generation")
        self.db.db.execute("INSERT INTO mail_outbound_v1 VALUES(?,?,?,?,?,?,?,NULL)",
                          ("second", "<second@example.invalid>", "{}", json.dumps(other), "other", 0, "accepted"))
        self.assertEqual(self.db.choose(msg(1, reply=MID, refs="<second@example.invalid>"))[1], "conflicting_thread_ids")

    def test_unconfirmed_and_definite_nonsend_held(self):
        for status in ("draft", "not_sent", "refused"):
            self.db.db.execute("UPDATE mail_outbound_v1 SET status=?", (status,))
            self.assertEqual(self.db.choose(msg(1, reply=MID))[1], "outbound_not_sent")

    def test_no_historical_replay(self):
        self.db.db.execute("UPDATE mail_outbound_v1 SET mid='<not-yet-registered@example.invalid>'")
        self.db.ingest(snap([msg(1, reply=MID)]))
        self.db.db.execute("UPDATE mail_outbound_v1 SET mid=?", (MID,))
        self.db.reroute()
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM mail_origin_deliveries_v1").fetchone()[0], 0)


class TransportTests(unittest.TestCase):
    def test_acceptance_sent_failure_partial_and_quit_failure(self):
        for refused in ({}, {"two@example.invalid": (550, b"no")}):
            smtp, imap = Mock(), Mock()
            smtp.return_value.__enter__ = Mock(return_value=smtp)
            smtp.return_value.__exit__ = Mock(return_value=False)
            smtp.send_message.return_value = refused
            imap.side_effect = OSError("synthetic Sent failure")
            result = transport.send({"messageId": MID, "to": ["one@example.invalid", "two@example.invalid"],
                                      "subject": "Synthetic", "body": "Synthetic body"},
                {"SYNCOPIA_MAIL_HOST": "mail.example.invalid", "SYNCOPIA_MAIL_USER": "sender@example.invalid",
                 "SYNCOPIA_MAIL_PASS": "synthetic-not-a-secret"}, smtp, imap)
            self.assertEqual(result["status"], "partial" if refused else "accepted")
            self.assertEqual(result["sentCopy"], "failed")
            smtp.send_message.assert_called_once()
            self.assertEqual(smtp.send_message.call_args.args[0]["Message-ID"], MID)

    def test_smtp_uncertainty_does_not_append_sent(self):
        smtp, imap = Mock(), Mock()
        smtp.return_value.__enter__ = Mock(return_value=smtp)
        smtp.return_value.__exit__ = Mock(return_value=False)
        smtp.send_message.side_effect = TimeoutError()
        result = transport.send({"messageId": MID, "to": ["one@example.invalid"], "subject": "Synthetic", "body": "Synthetic"},
            {"SYNCOPIA_MAIL_HOST": "mail.example.invalid", "SYNCOPIA_MAIL_USER": "sender@example.invalid",
             "SYNCOPIA_MAIL_PASS": "synthetic-not-a-secret"}, smtp, imap)
        self.assertEqual(result["status"], "unknown")
        imap.assert_not_called()
