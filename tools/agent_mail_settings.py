"""Operator-owned portable policy. Examples contain only synthetic identities."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("MAIL_ROUTER_CONFIG", ROOT / "examples/settings.example.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

ACCOUNT = CONFIG["account"]
PRIMARY_SENDERS = tuple(CONFIG["primary_senders"])
SECONDARY_SENDER = CONFIG["secondary_sender"]
if (not isinstance(ACCOUNT, str) or "@" not in ACCOUNT
        or len(PRIMARY_SENDERS) != 2
        or any(not isinstance(s, str) or "@" not in s for s in (*PRIMARY_SENDERS, SECONDARY_SENDER))):
    raise ValueError("mail_settings_identity_invalid")
SENDERS = (PRIMARY_SENDERS[0], SECONDARY_SENDER, PRIMARY_SENDERS[1])
DIRECT_CHATS = CONFIG["authorized_direct_chats"]
GROUP_TOPICS = CONFIG["authorized_group_topics"]
if (not isinstance(DIRECT_CHATS, list)
        or any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]*", v) for v in DIRECT_CHATS)
        or not isinstance(GROUP_TOPICS, list)
        or any(not isinstance(v, dict) or set(v) != {"chat_id", "thread_id"}
               or not isinstance(v["chat_id"], str) or not re.fullmatch(r"-[1-9][0-9]*", v["chat_id"])
               or not isinstance(v["thread_id"], str) or not re.fullmatch(r"[1-9][0-9]*", v["thread_id"])
               for v in GROUP_TOPICS)):
    raise ValueError("mail_settings_destinations_invalid")


def destination(parent):
    """Parse a destination only within explicit operator-authorized identities."""
    direct = re.fullmatch(r"agent:main:telegram:direct:([1-9][0-9]*):thread:\1:([1-9][0-9]*)", parent)
    if direct and direct[1] in DIRECT_CHATS:
        return direct[1], direct[2]
    group = re.fullmatch(r"agent:main:telegram:group:(-[1-9][0-9]*):topic:([1-9][0-9]*)", parent)
    if group and {"chat_id": group[1], "thread_id": group[2]} in GROUP_TOPICS:
        return group[1], group[2]
    raise ValueError("route_invalid_or_unverified")


def local_path(key, default):
    value = Path(CONFIG.get(key, default)).expanduser()
    return value if value.is_absolute() else ROOT / value


CONTENT_ROOT = local_path("content_root", "state/mail-content")
PROFILE_PATH = local_path("profile_path", "state/secrets/agent-mail.env")
OPENCLAW_CONFIG = local_path("openclaw_config", "state/secrets/openclaw.json")
CLI = CONFIG.get("openclaw_cli", "openclaw")
UI_BASE = CONFIG.get("control_ui_base", "https://control.example.invalid").rstrip("/")
if not isinstance(CLI, str) or not CLI or not UI_BASE.startswith("https://"):
    raise ValueError("mail_settings_adapter_invalid")
ERROR_PARENT = CONFIG["error_parent"]
ERROR_CHAT, ERROR_THREAD = destination(ERROR_PARENT)
MAIL_TOPIC = {"parent": ERROR_PARENT, "chat_id": ERROR_CHAT, "thread_id": ERROR_THREAD}
