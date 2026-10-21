"""Behavioral contract tests: no network, mailbox mutations, or durable state."""
import importlib.util
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "mail_snapshot", Path(__file__).resolve().parents[1] / "agent_mail_snapshot.py")
mail = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mail)

PLAIN = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "8BIT" 5 1 NIL NIL)'
HTML = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "8BIT" 8 1 NIL NIL)'
ATTACHED = b'("TEXT" "PLAIN" ("NAME" "secret.txt") NIL NIL "8BIT" 6 1 NIL ("ATTACHMENT" ("FILENAME" "secret.txt")))'
PDF = b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100)'


class FakeIMAP:
    def __init__(self, count=1, uidvalidity="77"):
        self.messages = {uid: {"sender": mail.SENDERS[uid % 2], "body": b"hello",
                               "structure": PLAIN} for uid in range(1, count + 1)}
        self.validity = uidvalidity
        self.high = count
        self.calls = []
        self.before_search = None
        self.after_uidnext = None
        self.fetch_fail = None
        self.sock = Mock()

    def select(self, mailbox, readonly=False):
        assert mailbox == '"INBOX"' and readonly is True
        self.calls.append(("EXAMINE",))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.validity.encode()]
        assert name == "UIDNEXT"
        value = self.high + 1
        if self.after_uidnext:
            self.after_uidnext(self)
        return name, [str(value).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        assert command in ("SEARCH", "FETCH"), "Unexpected/mutating IMAP command"
        if command == "SEARCH":
            assert args[0:2] == (None, "UID")
            assert args[3:] == (), "All UIDs must be searched, including unknown triage"
            if self.before_search:
                self.before_search(self)
            low, high = map(int, args[2].split(":"))
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages if low <= uid <= high)]
        uid, query = int(args[0]), args[1]
        if self.fetch_fail == uid:
            return "NO", [b"private server error"]
        message = self.messages[uid]
        if query == "(UID BODYSTRUCTURE)":
            return "OK", [b"1 (UID " + str(uid).encode() + b" BODYSTRUCTURE " + message["structure"] + b")"]
        assert "BODY.PEEK[" in query
        if "HEADER.FIELDS" in query:
            raw = message.get("headers") or (
                "From: " + message["sender"] + "\r\n"
                "Date: Sun, 18 Oct 2026 13:00:00 +0000\r\n"
                "Subject: =?UTF-8?B?0J/RgNC40LLQtdGC?=\r\n"
                f"Message-ID: <mail-{uid}@example.invalid>\r\n\r\n").encode()
        else:
            expected = message.get("section", "1")
            assert f"BODY.PEEK[{expected}]" in query
            raw = message["body"][:mail.BODY_BYTES]
        return "OK", [(f"1 (UID {uid} BODY[TEST] {{{len(raw)}}}".encode(), raw), b")"]

    def shutdown(self):
        self.calls.append(("DISCONNECT",))


class SnapshotTests(unittest.TestCase):
    def test_more_than_ten_paginated_without_loss(self):
        client = FakeIMAP(25)
        cursor, received = None, []
        for expected_size in (10, 10, 5):
            result, code = mail.snapshot(client, cursor)
            self.assertEqual(code, 0)
            self.assertEqual(len(result["messages"]), expected_size)
            self.assertEqual(result["has_more"], expected_size == 10)
            received.extend(msg["uid"] for msg in result["messages"])
            cursor = result["next_cursor"]
            self.assertEqual(cursor["last_uid"], received[-1])
        self.assertEqual(received, list(range(1, 26)))
        result, _ = mail.snapshot(client, cursor)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["next_cursor"], cursor)

    def test_baseline_freezes_boundary_before_arrival(self):
        client = FakeIMAP(4)
        def arrival(c):
            c.messages[5] = dict(c.messages[4])
            c.high = 5
        client.after_uidnext = arrival
        baseline, code = mail.snapshot(client, baseline=True)
        self.assertEqual(code, 0)
        self.assertEqual(baseline["next_cursor"]["last_uid"], 4)
        self.assertEqual(baseline["messages"], [])
        self.assertEqual(client.calls, [("EXAMINE",)])
        client.after_uidnext = None
        result, _ = mail.snapshot(client, baseline["next_cursor"])
        self.assertEqual([x["uid"] for x in result["messages"]], [5])

    def test_arrival_during_search_deferred_to_next_poll(self):
        client = FakeIMAP(1)
        def arrival(c):
            c.messages[2] = dict(c.messages[1])
            c.high = 2
        client.before_search = arrival
        result, _ = mail.snapshot(client)
        self.assertEqual([m["uid"] for m in result["messages"]], [1])
        result, _ = mail.snapshot(client, result["next_cursor"])
        self.assertEqual([m["uid"] for m in result["messages"]], [2])

    def test_uidvalidity_reset_is_explicit_nonzero_no_cursor(self):
        client = FakeIMAP(3, "88")
        result, code = mail.snapshot(client, {"uidvalidity": "77", "last_uid": 100})
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "uidvalidity_reset")
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(client.calls, [("EXAMINE",)])

    def test_exact_sender_rejects_substring_multi_and_malformed(self):
        senders = ["evil-" + mail.SENDERS[0], mail.SENDERS[0] + ".evil",
                   mail.SENDERS[0] + ", " + mail.SENDERS[1],
                   "broken <" + mail.SENDERS[0], '"source-a@example.invalid" <attacker@example.com>']
        for sender in senders:
            with self.subTest(sender=sender):
                client = FakeIMAP()
                client.messages[1]["sender"] = sender
                result, _ = mail.snapshot(client)
                self.assertEqual(result["messages"][0]["trust_class"], "unknown")
                self.assertEqual(result["messages"][0]["body"], "")
                self.assertEqual(result["next_cursor"]["last_uid"], 1)
                self.assertEqual(len([c for c in client.calls if c[0] == "FETCH"]), 1)

    def test_duplicate_from_rejected_without_body_fetch(self):
        client = FakeIMAP()
        client.messages[1]["headers"] = (
            f"From: {mail.SENDERS[0]}\r\nFrom: {mail.SENDERS[1]}\r\n\r\n").encode()
        result, _ = mail.snapshot(client)
        self.assertEqual(result["messages"][0]["trust_class"], "unknown")
        self.assertEqual(len([c for c in client.calls if c[0] == "FETCH"]), 1)

    def test_body_metadata_and_stable_identity(self):
        result, _ = mail.snapshot(FakeIMAP())
        message = result["messages"][0]
        self.assertEqual(message["body"], "hello")
        self.assertEqual(message["subject"], "Привет")
        self.assertEqual(message["key"], "77:1")
        self.assertEqual(message["message_id"], "<mail-1@example.invalid>")
        self.assertFalse(message["body_truncated"])

    def test_thread_headers_and_recipient_survive_readonly_fetch(self):
        client = FakeIMAP()
        client.messages[1]["headers"] = (
            f"From: {mail.SENDERS[0]}\r\nTo: {mail.ACCOUNT}\r\n"
            "Message-ID: <reply@example.com>\r\nIn-Reply-To: <parent@example.com>\r\n"
            "References: <root@example.com>\r\n <parent@example.com>\r\n\r\n").encode()
        message = mail.snapshot(client)[0]["messages"][0]
        self.assertEqual(message["to"], mail.ACCOUNT)
        self.assertEqual(message["in_reply_to"], "<parent@example.com>")
        self.assertIn("<root@example.com>", message["references"])
        query = next(c[2] for c in client.calls if c[0] == "FETCH")
        self.assertIn("IN-REPLY-TO REFERENCES", query)
        self.assertIn("BODY.PEEK", query)

    def test_html_preview_fetched_but_attachments_deferred(self):
        client = FakeIMAP()
        client.messages[1]["structure"] = b"(" + ATTACHED + b" " + PDF + b" " + HTML + b' "MIXED")'
        client.messages[1].update(section="3", body=b"<p>hello</p>")
        result, _ = mail.snapshot(client)
        self.assertEqual(result["messages"][0]["body_status"], "html_preview")
        self.assertEqual(result["messages"][0]["body"], "hello")
        self.assertEqual(len([c for c in client.calls if c[0] == "FETCH"]), 3)

    def test_nested_plain_part_selects_only_plain_section(self):
        client = FakeIMAP()
        client.messages[1]["structure"] = b"(" + ATTACHED + b" (" + HTML + b" " + PLAIN + b' "ALTERNATIVE") "MIXED")'
        client.messages[1]["section"] = "2.2"
        result, _ = mail.snapshot(client)
        self.assertEqual(result["messages"][0]["body"], "hello")

    def test_bounded_body_and_encoded_payload(self):
        for encoding, encoded in (("BASE64", b"aGVsbG8="),
                                   ("QUOTED-PRINTABLE", b"he=6Clo")):
            with self.subTest(encoding=encoding):
                client = FakeIMAP()
                client.messages[1].update(
                    body=encoded,
                    structure=PLAIN.replace(b'"8BIT" 5', f'"{encoding}" {len(encoded)}'.encode()))
                result, _ = mail.snapshot(client)
                self.assertEqual(result["messages"][0]["body"], "hello")
        client = FakeIMAP()
        client.messages[1].update(body=b"x" * 100000,
                                 structure=PLAIN.replace(b'"8BIT" 5', b'"8BIT" 100000'))
        result, _ = mail.snapshot(client)
        self.assertEqual(len(result["messages"][0]["body"]), mail.BODY_CHARS)
        self.assertTrue(result["messages"][0]["body_truncated"])

    def test_failure_in_batch_has_no_partial_success(self):
        client = FakeIMAP(11)
        client.fetch_fail = 5
        with self.assertRaisesRegex(mail.SnapshotError, "^fetch_failed$"):
            mail.snapshot(client)

    def test_empty_mailbox(self):
        result, code = mail.snapshot(FakeIMAP(0))
        self.assertEqual(code, 0)
        self.assertEqual(result["next_cursor"], {"uidvalidity": "77", "last_uid": 0})

    def test_cursor_validation(self):
        for value in ('{}', 'null', '{"uidvalidity":"77","last_uid":true}',
                      '{"uidvalidity":"77","last_uid":-1}', '{"uidvalidity":"0","last_uid":1}'):
            with self.assertRaises(Exception):
                mail.parse_cursor(value)
        self.assertEqual(mail.parse_cursor('{"uidvalidity":"77","last_uid":0}'),
                         {"uidvalidity": "77", "last_uid": 0})

    def test_expunge_race_missing_fetch_uid_is_failure(self):
        with self.assertRaisesRegex(mail.SnapshotError, "fetch_uid_missing"):
            mail.fetch(Mock(uid=Mock(return_value=("OK", [None]))), 1, "(UID BODYSTRUCTURE)")

    def test_bodystructure_literal_and_escaped_quote(self):
        data = [(b'1 (UID 1 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" {5}', b'UTF-8'),
                b') NIL "escaped \\" description" "8BIT" 5 1 NIL NIL))']
        part = mail.plain_part(mail.parse_structure(data))
        self.assertEqual(part, ("1", "UTF-8", "8BIT", 5))

    def test_secret_free_errors_and_deadline(self):
        with patch.object(mail, "load_profile", side_effect=RuntimeError("DO_NOT_PRINT_SECRET")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = mail.main([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "mail_check_failed")
        self.assertNotIn("DO_NOT_PRINT_SECRET", out.getvalue())
        with patch.object(mail, "DEADLINE_SECONDS", 0.05), patch.object(
                mail, "load_profile", side_effect=lambda: time.sleep(0.2)):
            out = io.StringIO()
            start = time.monotonic()
            with redirect_stdout(out):
                code = mail.main([])
        self.assertLess(time.monotonic() - start, 0.18)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "timeout")

    def test_only_expected_profile_identity(self):
        text = "AGENT_MAIL_USER=somebody-else@example.invalid\nAGENT_MAIL_PASS=SECRET"
        with patch.object(Path, "read_text", return_value=text):
            with self.assertRaisesRegex(mail.SnapshotError, "mailbox_identity_mismatch"):
                mail.load_profile()


if __name__ == "__main__":
    unittest.main()
