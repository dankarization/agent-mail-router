# Operator configuration

All checked-in addresses and destination IDs are synthetic. Examples are test data,
not proof that any live destination exists. Do not import an existing private route
map or mailbox state into Git.

## Policy

Copy `examples/settings.example.json` into ignored `config/settings.json` and set:

```sh
export MAIL_ROUTER_CONFIG="$PWD/config/settings.json"
```

Select policy **before** starting the Python process; modules read it at import.
An explicit path that is missing or invalid fails, without silently reverting.
The offline runner always selects repository examples regardless of this variable.

| Setting | Meaning |
| --- | --- |
| `account` | Exact IMAP login/mailbox identity |
| `primary_senders` | Two exact addresses displayed as Source A (legacy and current alias) |
| `secondary_sender` | One exact address displayed as Source B |
| `authorized_direct_chats` | Explicit string IDs allowed for direct thread parents |
| `authorized_group_topics` | Explicit objects with string `chat_id` and `thread_id`; exact pairs only |
| `error_parent` | Independent error destination, which must also pass the authorization list |
| `content_root` | Default deferred original/text storage |
| `profile_path` | IMAP profile file path |
| `openclaw_config` | Operator-supplied Telegram account JSON path |
| `openclaw_cli` | Existing OpenClaw executable; default PATH lookup, no installation |
| `control_ui_base` | HTTPS origin used to render session links |

Relative paths resolve against the repository root, not the current directory.
Defaults are beneath ignored `state/`. No paths into another checkout are assumed.
The current three-address attribution contract is preserved; this extraction does
not redesign sender registration.

## Routes and verification

Runtime default: `state/mail-routing/routes.json`. Use `--routes FILE` to select
another operator-owned map. Schema version 1 has a `routes` array:

- Unique `id`, `title`, authorized `parent`, and nonempty `evidence`.
- `children`: known verified programmer subagent/dashboard session keys, at most 8.
- `message_ids`: exact Message-ID anchors; `task_refs`: exact task references.
- `paused`: keep context without authorizing resume.
- Optional `fallback: true` on at most one general route; it cannot have task
  refs, Message-ID anchors, children or paused state.

Direct parent syntax: `agent:main:telegram:direct:CHAT:thread:CHAT:TOPIC`.
Group parent syntax: `agent:main:telegram:group:NEGATIVE_CHAT:topic:TOPIC`.
A group route requires both the exact policy pair and route evidence; adjacent
groups/topics are not implicitly authorized. Child session keys retain the existing
programmer agent contract, and runtime validation checks the parent relationship.

Verify actual destination metadata and child lineage before writing operator
evidence. A nonempty evidence string is an audit record, not cryptographic proof.
Gateway validation uses bounded complete `sessions.list`; context injection uses
`chat.inject`, with receipts and positive-marker reconciliation via `chat.history`.
These adapters require a compatible, already authenticated OpenClaw CLI; they
are external dependencies for real delivery, not for tests.

## Credentials (never checked in)

The configured IMAP profile is a UTF-8 key=value file:

```text
AGENT_MAIL_USER=router@example.invalid
AGENT_MAIL_HOST=imap.example.invalid
AGENT_MAIL_PASS=REPLACE_LOCALLY
```

The user must exactly match configured `account`. IMAP uses TLS with standard
certificate validation. Keep the actual file private (0600) in an ignored directory.

The Telegram adapter reads an operator-supplied JSON file with the existing
`channels.telegram.accounts.default` shape: `botToken`, optional `enabled` and
optional `apiRoot`. No token example or private account file is included.
Disabled channel/account fails closed. Only a plain token string is supported;
unresolved secret references are rejected. API root is official HTTPS or an
explicit loopback HTTP port. The adapter makes one request, rejects redirects,
checks chat/topic/message receipt and rechecks quiet hours immediately before send.

## First checkpoint

There is **no historical default UIDVALIDITY/UID**. A new Store rejects a missing or
invalid initial cursor before polling. For a new mailbox, an authorized operator
can obtain a baseline (skips prior mail without reading bodies):

```sh
python3 tools/agent_mail_snapshot.py --baseline
```

This is a real authenticated IMAP operation, not an offline quick-start command.
Review its `next_cursor`, then explicitly pass it to a first router tick as
`--trigger-state-json '{"cursor":{"uidvalidity":"VALIDITY","last_uid":UID}}'`,
with real numeric values. Selecting a replay-from-zero policy is a separate
operator decision, never inferred. Subsequent ticks use the durable local cursor;
do not reset state to retry delivery. A UIDVALIDITY change stops admission.

The stateless snapshot tool, if deliberately invoked without `--baseline` or
`--cursor`, still scans from zero by its original API contract. Do not confuse
that tool with the router's explicit fresh-install checkpoint requirement.
