"""All-mail report-only policy and operator-owned outbound-origin registration.

Thread headers correlate, never authenticate. Screening is a bounded heuristic,
not proof of safety; suspicious mail is still reported as untrusted data.
"""
import json
import re
from pathlib import Path

MODE = "external_report_only"
REASON = "registered_outbound_thread"
MID = re.compile(r"<[^<>\s]{1,998}>")
PATTERNS = {
    "instruction_override": r"(?i)(ignore|disregard|override).{0,60}(previous|prior|system|instructions)|игнорир.{0,60}(инструкц|предыдущ)",
    "role_or_tool_injection": r"(?i)(<\|(?:system|assistant|im_start)\|>|\[INST\]|system\s*prompt|developer\s*message|tool_calls?\s*[:=])",
    "credential_or_execution_request": r"(?i)(reveal|send|print|export).{0,60}(secret|password|token|credential)|(?:curl|wget).{0,100}\|\s*(?:sh|bash)|(?:exec|eval)\s*\(",
}


def ids(value):
    return MID.findall(value or "")


def authorized_destinations(cfg):
    found = set()
    grants = cfg.get("authorized_report_destinations", [])
    if not isinstance(grants, list) or len(grants) > 100:
        raise ValueError("report_destination_grants_invalid")
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != {"parent", "evidence"}
                or not isinstance(grant["parent"], str)
                or not re.fullmatch(r"agent:main:telegram:group:-[1-9][0-9]*:topic:[1-9][0-9]*", grant["parent"])
                or grant["parent"] in found or not isinstance(grant["evidence"], str) or not grant["evidence"]):
            raise ValueError("report_destination_grants_invalid")
        found.add(grant["parent"])
    return found


def validate(cfg):
    authorized_destinations(cfg)
    outgoing = {}
    for route in cfg["routes"]:
        if route.get("intake_policy") not in (None, MODE):
            raise ValueError("external_policy_invalid")
        threads = route.get("outbound_threads", [])
        if not isinstance(threads, list) or len(threads) > 100:
            raise ValueError("outbound_registration_invalid")
        if route.get("intake_policy") == MODE and (route.get("children") or route.get("fallback") or route.get("task_refs") or route.get("message_ids") or not threads):
            raise ValueError("external_route_invalid")
        for thread in threads:
            if not isinstance(thread, dict) or set(thread) != {"message_id", "recipient", "task_ref", "sent_evidence"}:
                raise ValueError("outbound_registration_invalid")
            mid = thread["message_id"]
            if (not isinstance(mid, str) or ids(mid) != [mid] or mid in outgoing
                    or not isinstance(thread["recipient"], str)
                    or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+", thread["recipient"])
                    or not isinstance(thread["task_ref"], str) or len(thread["task_ref"]) > 100
                    or not isinstance(thread["sent_evidence"], str) or not thread["sent_evidence"]
                    or route.get("fallback") or not route.get("evidence")):
                raise ValueError("outbound_registration_invalid")
            outgoing[mid] = (route["id"], thread)
    if len(outgoing) > 100:
        raise ValueError("outbound_registration_limit")
    for route in cfg["routes"]:
        for mid in route.get("message_ids", []):
            if mid in outgoing and outgoing[mid][0] != route["id"]:
                raise ValueError("outbound_anchor_collision")
    return outgoing


def origins(message, cfg):
    """Only IRT/References to an operator-registered sent ID select an origin."""
    outgoing = validate(cfg)
    refs = set(ids(message.get("in_reply_to")) + ids(message.get("references")))
    return {outgoing[mid][0] for mid in refs if mid in outgoing}


def screen(message, text=None, complete=False):
    text = (message.get("subject", "") + "\n" + message.get("body", "")) if text is None else text
    # Bound CPU independently of attachment/body size. Unscanned content is explicit.
    limit = 1024 * 1024
    flags = sorted(name for name, pattern in PATTERNS.items() if re.search(pattern, text[:limit]))
    return {"risk": "suspicious" if flags else "no_indicators_not_safe",
            "indicators": flags, "screened_chars": min(len(text), limit),
            "coverage": "full_text" if complete and len(text) <= limit else "partial",
            "remaining_chars": max(0, len(text) - limit) if complete else None,
            "attachment_boundary": "attachments require clean extraction; no execution",
            "guarantee": False}


def report_allowed(message, state):
    """Content/Seen requires the exact admitted row, never a mail-provided flag."""
    import sqlite3
    db = None
    try:
        db = sqlite3.connect((Path(state) / "mail.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        row = db.execute("SELECT route,reason,payload FROM mail WHERE key=?", (message["key"],)).fetchone()
        if not row or not row[0] or json.loads(row[2]) != message or row[1] not in (
                REASON, "general_external_report", "thread_identity", "explicit_task_reference", "general_fallback"):
            return False
        cfg = json.loads((Path(state) / "routes.json").read_text())
        validate(cfg)
        route = next((r for r in cfg["routes"] if r["id"] == row[0]), None)
        return bool(route and (message.get("sender") not in __import__("agent_mail_content").SENDERS
                              or route.get("intake_policy") == MODE))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False
    finally:
        if db is not None:
            db.close()
