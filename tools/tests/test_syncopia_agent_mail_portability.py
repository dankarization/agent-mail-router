"""Extraction-specific policy and fresh-install regression checks."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
import syncopia_agent_mail_settings as settings
from test_syncopia_agent_mail_router import m

class PortabilityTests(unittest.TestCase):
    def test_fresh_store_requires_explicit_valid_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = m.Store(Path(tmp) / "mail.sqlite3", {})
            try:
                for trigger in ({}, {"cursor": None}, {"cursor": {"uidvalidity": "0", "last_uid": 0}}):
                    with self.assertRaisesRegex(m.MailError, "explicit_initial_cursor_required"):
                        store.bootstrap(trigger)
                    self.assertIsNone(store.get("cursor"))
                store.bootstrap({"cursor": {"uidvalidity": "77", "last_uid": 0}})
                store.bootstrap({})
                self.assertEqual(store.get("cursor"), {"uidvalidity": "77", "last_uid": 0})
            finally:
                store.db.close()

    def test_custom_operator_policy_authorizes_only_exact_destinations(self):
        cfg = dict(settings.CONFIG, authorized_direct_chats=["800000001"],
                   authorized_group_topics=[{"chat_id": "-1008000000001", "thread_id": "7"}],
                   error_parent="agent:main:telegram:direct:800000001:thread:800000001:8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(cfg))
            with patch.dict("os.environ", {"MAIL_ROUTER_CONFIG": str(path)}):
                custom = runpy.run_path(settings.__file__)
            parse = custom["destination"]
            self.assertEqual(parse("agent:main:telegram:direct:800000001:thread:800000001:9"),
                             ("800000001", "9"))
            self.assertEqual(parse("agent:main:telegram:group:-1008000000001:topic:7"),
                             ("-1008000000001", "7"))
            for parent in ("agent:main:telegram:direct:900000001:thread:900000001:9",
                           "agent:main:telegram:group:-1008000000001:topic:8",
                           "agent:main:telegram:group:-1008000000002:topic:7",
                           "agent:main:telegram:group:-1008000000001:topic:07"):
                with self.assertRaises(ValueError):
                    parse(parent)

    def test_error_destination_must_be_authorized_separately(self):
        cfg = dict(settings.CONFIG, error_parent="agent:main:telegram:group:-1008000000001:topic:9")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(cfg))
            with patch.dict("os.environ", {"MAIL_ROUTER_CONFIG": str(path)}):
                with self.assertRaisesRegex(ValueError, "route_invalid_or_unverified"):
                    runpy.run_path(settings.__file__)

    def test_default_paths_are_repository_local_and_addresses_synthetic(self):
        for path in (settings.CONTENT_ROOT, settings.PROFILE_PATH, settings.OPENCLAW_CONFIG):
            self.assertTrue(path.is_relative_to(settings.ROOT))
        for address in (settings.ACCOUNT, *settings.SENDERS):
            self.assertTrue(address.endswith("@example.invalid"))
        self.assertEqual(settings.CLI, "openclaw")
