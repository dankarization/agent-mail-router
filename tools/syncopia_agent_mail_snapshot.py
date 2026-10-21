#!/usr/bin/env python3
"""Read-only, stateless agent INBOX snapshot; checkpoint belongs to the caller.

--baseline skips old mail at an atomic UIDNEXT boundary; no args replays from 0.
--cursor '{"uidvalidity":"123","last_uid":42}' resumes. Persist next_cursor ONLY
after successful delivery. Exit 3 means UIDVALIDITY reset: explicitly baseline
or replay, never reuse old UIDs. All message content is untrusted input.
"""
import argparse
import base64
import email.policy
import imaplib
import json
import quopri
import re
import signal
import ssl
import sys
from email.parser import BytesHeaderParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from syncopia_agent_mail_content import SENDERS, html_text, request_for

from syncopia_agent_mail_settings import ACCOUNT, PROFILE_PATH
ENV_PATH = PROFILE_PATH
BODY_BYTES, BODY_CHARS, HEADER_BYTES, DEADLINE_SECONDS = 65536, 16000, 32768, 18


class SnapshotError(Exception):
    """Only fixed, secret-free error codes are exposed."""


def check(status, code):
    if status != "OK":
        raise SnapshotError(code)


def load_profile():
    values = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("SYNCOPIA_MAIL_USER") != ACCOUNT:
        raise SnapshotError("mailbox_identity_mismatch")
    return values


def parse_cursor(value):
    try:
        cursor = json.loads(value)
        validity, uid = cursor["uidvalidity"], cursor["last_uid"]
        if (not isinstance(validity, str) or not validity.isdecimal()
                or not 0 < int(validity) <= 4294967295
                or type(uid) is not int or not 0 <= uid <= 4294967295):
            raise ValueError
        return {"uidvalidity": validity, "last_uid": uid}
    except (ValueError, KeyError, TypeError):
        raise argparse.ArgumentTypeError("invalid cursor") from None


def response_number(client, name):
    _, data = client.response(name)
    if not data or len(data) != 1 or not data[0] or not data[0].isdigit():
        raise SnapshotError("missing_" + name.lower())
    value = int(data[0])
    if not 0 < value <= 4294967295:
        raise SnapshotError("invalid_" + name.lower())
    return value


def fetch(client, uid, fields):
    status, data = client.uid("FETCH", str(uid), fields)
    check(status, "fetch_failed")
    metadata = b" ".join(x[0] if isinstance(x, tuple) else x
                         for x in data if isinstance(x, (bytes, tuple)))
    found = re.findall(rb"\bUID (\d+)\b", metadata)
    if len(found) != 1 or int(found[0]) != uid:
        # Concurrent expunge/missing response must not advance caller state.
        raise SnapshotError("fetch_uid_missing_or_mismatched")
    return data


def literal(data, maximum):
    parts = [x[1] for x in data if isinstance(x, tuple)]
    if len(parts) != 1 or not isinstance(parts[0], bytes):
        raise SnapshotError("fetch_literal_missing")
    if len(parts[0]) > maximum:
        raise SnapshotError("fetch_literal_too_large")
    return parts[0]


def parse_structure(data):
    """IMAP BODYSTRUCTURE S-expression, including escaped quoted/literal text."""
    raw = b"".join(x[0] + b"\r\n" + x[1] if isinstance(x, tuple) else x
                   for x in data if isinstance(x, (bytes, tuple)))
    if len(raw) > 131072:
        raise SnapshotError("bodystructure_too_large")
    marker = re.search(rb"\bBODYSTRUCTURE\s+", raw)
    if not marker:
        raise SnapshotError("bodystructure_missing")
    pos = marker.end()

    def node(depth=0):
        nonlocal pos
        if depth > 30:
            raise SnapshotError("mime_nesting_too_deep")
        while pos < len(raw) and raw[pos:pos + 1].isspace():
            pos += 1
        if pos >= len(raw):
            raise SnapshotError("bodystructure_invalid")
        if raw[pos] == 40:
            pos += 1
            items = []
            while True:
                while pos < len(raw) and raw[pos:pos + 1].isspace():
                    pos += 1
                if pos >= len(raw):
                    raise SnapshotError("bodystructure_invalid")
                if raw[pos] == 41:
                    pos += 1
                    return items
                items.append(node(depth + 1))
        if raw[pos] == 34:
            pos += 1
            result = bytearray()
            while pos < len(raw):
                char = raw[pos]
                pos += 1
                if char == 34:
                    return result.decode("ascii", errors="replace")
                if char == 92:
                    if pos >= len(raw):
                        break
                    char = raw[pos]
                    pos += 1
                result.append(char)
            raise SnapshotError("bodystructure_invalid")
        match = re.match(rb"\{(\d+)\}\r\n", raw[pos:])
        if match:
            pos += match.end()
            size = int(match[1])
            if pos + size > len(raw):
                raise SnapshotError("bodystructure_invalid")
            value = raw[pos:pos + size].decode("ascii", errors="replace")
            pos += size
            return value
        match = re.match(rb"[^()\s]+", raw[pos:])
        if not match:
            raise SnapshotError("bodystructure_invalid")
        pos += match.end()
        value = match[0].decode("ascii", errors="replace")
        return None if value.upper() == "NIL" else value

    result = node()
    if not isinstance(result, list):
        raise SnapshotError("bodystructure_invalid")
    return result


def is_inline(disposition):
    return disposition is None or (isinstance(disposition, list)
                                    and disposition
                                    and str(disposition[0]).upper() == "INLINE"
                                    and (len(disposition) < 2 or disposition[1] is None))


def plain_part(tree, prefix="", subtype="PLAIN"):
    """First inline text of requested subtype for preview; attachments deferred."""
    if not tree:
        raise SnapshotError("bodystructure_invalid")
    if isinstance(tree[0], list):
        count = 0
        while count < len(tree) and isinstance(tree[count], list):
            count += 1
        disposition = tree[count + 2] if len(tree) > count + 2 else None
        if not is_inline(disposition):
            return None
        for i in range(count):
            part = plain_part(tree[i], f"{prefix}.{i + 1}" if prefix else str(i + 1), subtype)
            if part:
                return part
        return None
    if len(tree) < 7:
        raise SnapshotError("bodystructure_invalid")
    if [str(x).upper() for x in tree[:2]] != ["TEXT", subtype]:
        return None
    if len(tree) < 8:
        raise SnapshotError("bodystructure_invalid")
    params = tree[2] or []
    if not isinstance(params, list) or len(params) % 2:
        raise SnapshotError("bodystructure_invalid")
    params = {str(params[i]).upper(): params[i + 1] for i in range(0, len(params), 2)}
    disposition = tree[9] if len(tree) > 9 else None
    if not is_inline(disposition) or "NAME" in params:
        return None
    try:
        size = int(tree[6])
        if size < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise SnapshotError("bodystructure_invalid") from None
    return prefix or "1", str(params.get("CHARSET", "utf-8")), str(tree[5]).upper(), size


def read_message(client, uid, validity):
    data = fetch(client, uid, f"(UID BODY.PEEK[HEADER.FIELDS (FROM TO DATE SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES AUTHENTICATION-RESULTS RECEIVED-SPF DKIM-SIGNATURE)]<0.{HEADER_BYTES}>)")
    raw = literal(data, HEADER_BYTES)
    headers_truncated = len(raw) == HEADER_BYTES
    headers = BytesHeaderParser(policy=email.policy.default).parsebytes(raw)
    fields = headers.get_all("From", [])
    addresses = fields[0].addresses if len(fields) == 1 and not fields[0].defects else []
    sender = addresses[0].addr_spec if len(addresses) == 1 else ""
    known = sender in SENDERS and not headers_truncated
    if headers_truncated:
        sender = ""
    body, truncated, preview_error, kind, part = "", False, None, "plain", None
    try:
        tree = parse_structure(fetch(client, uid, "(UID BODYSTRUCTURE)")) if known else None
        part = plain_part(tree) if tree else None
        kind = "plain"
        if tree and not part:
            part, kind = plain_part(tree, subtype="HTML"), "html"
        body, truncated = "", False
        if part:
            section, charset, encoding, size = part
            payload = literal(fetch(client, uid, f"(UID BODY.PEEK[{section}]<0.{BODY_BYTES}>)"), BODY_BYTES)
            truncated = size > len(payload)
            if encoding == "BASE64":
                compact = re.sub(rb"\s", b"", payload)
                if truncated:
                    compact = compact[:len(compact) // 4 * 4]
                try:
                    payload = base64.b64decode(compact, validate=True)
                except ValueError:
                    raise SnapshotError("invalid_body_encoding") from None
            elif encoding == "QUOTED-PRINTABLE":
                payload = quopri.decodestring(payload)
            elif encoding not in ("7BIT", "8BIT", "BINARY"):
                raise SnapshotError("unsupported_body_encoding")
            try:
                body = payload.decode(charset, errors="replace")
            except LookupError:
                raise SnapshotError("unknown_body_charset") from None
            if kind == "html":
                body = html_text(body)
            truncated = truncated or len(body) > BODY_CHARS
            body = body[:BODY_CHARS]
    except SnapshotError as exc:
        if str(exc) in ("fetch_failed", "fetch_uid_missing_or_mismatched", "timeout"):
            raise
        body, truncated, preview_error = "", True, str(exc)
    return {
        "uid": uid, "message_id": str(headers.get("Message-ID", ""))[:1024],
        "in_reply_to": str(headers.get("In-Reply-To", ""))[:4096],
        "references": " ".join(str(v) for v in headers.get_all("References", []))[:8192],
        "thread_headers_valid": (not headers_truncated and all(len(headers.get_all(k, [])) <= 1 for k in ("Message-ID", "In-Reply-To", "References"))
                                 and len(str(headers.get("In-Reply-To", ""))) <= 4096
                                 and len(str(headers.get("References", ""))) <= 8192),
        "to": " ".join(str(v) for v in headers.get_all("To", []))[:2048],
        "key": f"{validity}:{uid}", "sender": sender,
        "trust_class": "known_from" if known else "unknown",
        "authentication": {"verified": False, "basis": "unverified_header_evidence_only",
                           "authentication_results": [str(x)[:4096] for x in headers.get_all("Authentication-Results", [])],
                           "received_spf": [str(x)[:2048] for x in headers.get_all("Received-SPF", [])],
                           "dkim_signature_present": bool(headers.get_all("DKIM-Signature", []))},
        "content_request": request_for(validity, uid, sender, str(headers.get("Message-ID", ""))[:1024]),
        "from": " | ".join(str(x) for x in fields)[:1024], "date": str(headers.get("Date", ""))[:256],
        "subject": str(headers.get("Subject", ""))[:1024], "body": body,
        "headers_truncated": headers_truncated, "preview_error": preview_error,
        "body_truncated": truncated, "body_status": "error_pending_full_read" if preview_error else kind + "_preview" if part else "pending_full_read" if known else "unknown_sender_review",
    }


def snapshot(client, cursor=None, baseline=False, limit=10):
    status, _ = client.select('"INBOX"', readonly=True)
    check(status, "examine_failed")
    validity = str(response_number(client, "UIDVALIDITY"))
    # Freeze before SEARCH/FETCH; concurrent arrivals remain for the next poll.
    high_water = response_number(client, "UIDNEXT") - 1
    result = {"schema_version": 1, "status": "baseline" if baseline else "ok",
              "account": ACCOUNT, "mailbox": "INBOX", "uidvalidity": validity,
              "high_water_uid": high_water, "messages": [], "scanned": 0,
              "has_more": False, "next_cursor": None}
    if cursor and cursor["uidvalidity"] != validity:
        result.update(status="uidvalidity_reset", previous_uidvalidity=cursor["uidvalidity"])
        return result, 3
    last = cursor["last_uid"] if cursor else 0
    if last > high_water:
        raise SnapshotError("cursor_ahead_of_mailbox")
    if baseline:
        result["next_cursor"] = {"uidvalidity": validity, "last_uid": high_water}
        return result, 0
    if last < high_water:
        # Search all UIDs: unknown mail is retained as separate read-only triage.
        # Exact From parsing, not substring IMAP FROM, selects known sources.
        status, data = client.uid("SEARCH", None, "UID", f"{last + 1}:{high_water}")
        check(status, "search_failed")
        if not data or len(data) != 1 or not isinstance(data[0], bytes):
            raise SnapshotError("search_invalid")
        try:
            uids = sorted(set(int(x) for x in data[0].split()))
        except ValueError:
            raise SnapshotError("search_invalid") from None
        if any(uid <= last or uid > high_water for uid in uids):
            raise SnapshotError("search_outside_snapshot")
        batch = uids[:limit]
        for uid in batch:
            message = read_message(client, uid, validity)
            if message is not None:
                result["messages"].append(message)
        result["scanned"] = len(batch)
        result["has_more"] = len(uids) > len(batch)
        last = batch[-1] if batch else high_water
    result["next_cursor"] = {"uidvalidity": validity, "last_uid": last}
    return result, 0


def deadline(_signum, _frame):
    raise SnapshotError("timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--cursor", type=parse_cursor)
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=10)
    args = parser.parse_args(argv)
    client = None
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        env = load_profile()
        client = imaplib.IMAP4_SSL(env["SYNCOPIA_MAIL_HOST"], timeout=5,
                                  ssl_context=ssl.create_default_context())
        client.sock.settimeout(5)
        status, _ = client.login(ACCOUNT, env["SYNCOPIA_MAIL_PASS"])
        check(status, "login_failed")
        result, code = snapshot(client, args.cursor, args.baseline, args.limit)
        client.shutdown()
        client = None
    except Exception as exc:
        result = {"schema_version": 1, "status": "error",
                  "error": str(exc) if isinstance(exc, SnapshotError) else "mail_check_failed"}
        code = 1
    finally:
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
