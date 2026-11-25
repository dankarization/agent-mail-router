"""Private transport worker: no origin registration and no session-key input.

Only the installed adapter calls this worker after durable human confirmation.
OS account, installed source and operator credential file are trusted. This is
not an authenticated API for arbitrary shell callers.
"""
import imaplib
import json
import os
from pathlib import Path
import smtplib
import stat
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime


def credentials(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 16384:
            raise ValueError("private_credentials_required")
        result = dict(line.strip().split("=", 1) for line in f if line.strip() and not line.lstrip().startswith("#"))
    for key in ("SYNCOPIA_MAIL_HOST", "SYNCOPIA_MAIL_USER", "SYNCOPIA_MAIL_PASS"):
        if not result.get(key):
            raise ValueError("credentials_missing")
    return result


def send(envelope, env, smtp_factory=smtplib.SMTP_SSL, imap_factory=imaplib.IMAP4_SSL):
    msg = EmailMessage()
    msg["From"] = env["SYNCOPIA_MAIL_USER"]
    msg["To"] = ", ".join(envelope["to"])
    msg["Subject"] = envelope["subject"]
    msg["Message-ID"] = envelope["messageId"]
    msg["Date"] = format_datetime(datetime.now().astimezone())
    msg.set_content(envelope["body"], subtype="plain", charset="utf-8")
    attempted = False
    result = {"status": "not_sent", "sentCopy": "not_attempted"}
    try:
        with smtp_factory(env["SYNCOPIA_MAIL_HOST"], 465, timeout=15) as smtp:
            smtp.login(env["SYNCOPIA_MAIL_USER"], env["SYNCOPIA_MAIL_PASS"])
            attempted = True
            try:
                refused = smtp.send_message(msg, from_addr=env["SYNCOPIA_MAIL_USER"], to_addrs=envelope["to"])
            except smtplib.SMTPRecipientsRefused:
                return {"status": "refused", "sentCopy": "not_attempted"}
            result["status"] = "partial" if refused else "accepted"
            result["refused"] = sorted(refused)
    except Exception:
        if result["status"] not in ("accepted", "partial"):
            return {"status": "unknown" if attempted else "not_sent", "sentCopy": "not_attempted"}
        # SMTP QUIT can fail after DATA was positively acknowledged. Preserve acceptance.
    try:
        with imap_factory(env["SYNCOPIA_MAIL_HOST"], timeout=15) as imap:
            imap.login(env["SYNCOPIA_MAIL_USER"], env["SYNCOPIA_MAIL_PASS"])
            status, _ = imap.append("Sent", None, imaplib.Time2Internaldate(datetime.now().timestamp()), msg.as_bytes())
            result["sentCopy"] = "ok" if status == "OK" else "failed"
    except Exception:
        result["sentCopy"] = "failed"
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(20000)
        envelope = json.loads(raw)
        if set(envelope) != {"messageId", "to", "subject", "body"}:
            raise ValueError("transport_input_invalid")
        result = send(envelope, credentials(Path(sys.argv[1])))
    except Exception:
        # Never emit credentials, provider text, message bodies or raw errors.
        result = {"status": "not_sent", "sentCopy": "not_attempted"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
