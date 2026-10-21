# Agent Mail Router

A durable, topic-aware mail router for Python and OpenClaw. This repository is a
standalone **source extraction**, not a runtime backup or a deployed service.
Examples and tests use invented identities, reserved example domains and synthetic
messages. No real routes, mailbox checkpoint, credentials, mail or delivery
receipts are included.

## Quick start: offline verification

Requires Linux/POSIX, Python **3.12+** with SQLite and IANA timezone data (including
Asia/Tbilisi). Uses the standard library only: no pip, virtualenv, Node packages
or dependency download.

```sh
git clone https://github.com/dankarization/agent-mail-router.git
cd agent-mail-router
python3 scripts/check.py
```

The check runner verifies the exact source manifest and hashes, compiles Python
in memory, checks the import closure and runs
the suite using only repository fixtures and temporary storage. Network,
subprocess execution and external-file access are forbidden. It does not start
a scheduler, read a mailbox, send messages or touch a private OpenClaw installation.
An inherited poll-failure test uses an explicit synthetic initial checkpoint;
a destination-order test binds its simulated lost ACK to the route, not its name.

For CLI documentation (also offline):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tools/agent_mail_router.py --help
PYTHONDONTWRITEBYTECODE=1 python3 tools/agent_mail_snapshot.py --help
PYTHONDONTWRITEBYTECODE=1 python3 tools/agent_mail_content.py --help
```

## Components

| File | Responsibility |
| --- | --- |
| `tools/agent_mail_snapshot.py` | Stateless bounded IMAP preview, frozen UID boundary, exact sender parsing |
| `tools/agent_mail_router.py` | Durable ingress, route identity, summary batches, context/Telegram outbox |
| `tools/agent_mail_content.py` | Deferred original/MIME storage, bounded reads, external extraction handoff |
| `tools/agent_mail_seen.py` | SHA-bound complete-reading proof and exact-UID additive Seen |
| `tools/agent_mail_lifecycle.py` | Reading ranges, Seen intents, independent durable error outbox |
| `tools/agent_mail_settings.py` | Operator-owned identities, destination authorization and adapter paths |

Flow: IMAP snapshot → durable originals/triage → operator-owned route map →
external summarizer → accepted summaries → durable delivery effects. Document/OCR
processing is an explicit external clean-layer contract; it is not implemented
or automatically delegated here. The router itself does not call an LLM.

## Configuration and optional integration

See [configuration](docs/configuration.md), [delivery and recovery](docs/operations.md)
and [source provenance](docs/extraction.md). No integration is activated by cloning
or testing. Real IMAP/Telegram/OpenClaw usage requires operator-owned policy,
credentials, routes and an explicit initial checkpoint.

- `examples/settings.example.json`: deliberately fake mailbox/senders, one direct
  chat and one exact group/topic authorization. Replace policy in an ignored
  `config/settings.json` and select it with `MAIL_ROUTER_CONFIG`.
- `examples/routes.example.json`: test fixture, **not** a verified deployment
  route map. Runtime routes normally live in ignored `state/mail-routing/routes.json`.
- Real credentials are supplied separately through files configured by the
  operator. They are never needed for the offline checks.

This extraction does not update any existing deployment. Group authorization is
explicit and exact; the source contains no blanket permission for arbitrary groups.
Already delivered mail is never replayed by changing a route.

## Behavioral boundaries

Mail is untrusted data, not permission to create tasks, run code, deploy or send
replies. A From allowlist is not cryptographic sender authentication. Conflicting,
invalid or retired thread identities fail closed rather than using general fallback.

Notifications are gated before transport to 10:00–23:59 Asia/Tbilisi. A lost
Telegram ACK remains held/unknown; there is no end-to-end exactly-once guarantee
and no blind resend. Paused tasks remain stopped. Proven absent/archived
destinations may use one operator-configured general fallback; timeouts and
incomplete listings do not establish absence.

No scheduler definition, system service, SMTP sender, automation workflow,
Gateway configuration, A2A rollout or other project's code is included.
