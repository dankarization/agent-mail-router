# Source extraction and scope

The initial tree was extracted from the current topic-aware Python mail helpers
on 2026-10-21. It has a new Git root and initial history; no source workspace
history, runtime configuration or proof archive is imported.

## Imported allowlist

Five source helpers and their four existing test modules:

```text
tools/agent_mail_content.py
tools/agent_mail_lifecycle.py
tools/agent_mail_router.py
tools/agent_mail_seen.py
tools/agent_mail_snapshot.py
tools/tests/test_agent_mail_content.py
tools/tests/test_agent_mail_lifecycle.py
tools/tests/test_agent_mail_router.py
tools/tests/test_agent_mail_snapshot.py
```

Source and tests are adapted, not byte-identical runtime exports:

- Replace deployment emails, IDs, task references and fixture labels with invented
  values; fixtures are newly constructed, not copied from a live route map.
- Move operator identity/authorization/adapter paths to a separate settings module.
- Preserve explicit exact group/topic authorization; add no wildcard group policy.
- Replace private UI/CLI/config/spool paths with portable settings.
- Remove historical startup cursor; require an explicit validated first cursor.
- Remove tests' dependency on a private runtime routes file.
- Bind the simulated lost-ACK result to its destination rather than route-name
  sort order, and explicitly initialize the poll-failure fixture.

Added: portable settings and example files, offline verification, extraction-policy
regressions, configuration/recovery documentation and the publication manifest.
The full inherited behavioral suite remains present with synthetic data.

## Excluded

No runtime routes, real mailbox/session identities, messages/EML, SQLite databases,
state, cursor/receipts, credentials, config snapshots, private proof snapshots,
scheduler files, deployment units, Gateway plugins, A2A implementation, unrelated
project sources, node_modules, environment or copied Git objects.

Only standard-library runtime imports and local repository modules are used.
Document extraction and real OpenClaw/IMAP/Telegram adapters remain optional
external integration contracts; no dependency environment is materialized.

No license grant is added by this extraction. No bundled third-party dependencies
or copied third-party license files are introduced. Repository access does not
itself grant redistribution permission.
