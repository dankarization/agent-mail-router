# Human-confirmed outbound origin (candidate, disabled until installed)

This extends the existing router; it is not a second mail router. Existing
operator routes, sender policy, fallback, locks, cursor, original mail and delivery
receipts are not replaced. No historical replay or automatic activation.

## Trusted path and deliberate workflow change

The legacy shell sender has no authenticated runtime origin. A session key in
CLI arguments, environment, email headers, model output or a hand-written
registration file is NOT authorization.

The optional native plugin registers a version-2 `mail_prepare` tool. The host
factory supplies the actual agent, session key, incarnation and requester; the
tool schema has no origin field. The tool prepares an immutable recipient,
subject and plain-text body with a caller-stable operation ID. It **does not
send**. It returns the complete bounded draft, its digest and a random
confirmation command. The human reviews it and personally invokes
`/mailsend TOKEN` in that same session within ten minutes. A model must never
execute or send that command on the human's behalf.

The host's native command authorization requires `operator.write`, an
authorized owner, current owner authority, the same sender/channel/account and
exact session incarnation. The plugin does not resolve approvals itself, read
transcripts for consent, interpret email requests as consent, or persist an
allow-always grant. This uses existing native command authorization, NOT a new
ACL or an approval-policy change. Third-party plugins cannot assume access to
the privileged runtime Gateway RPC surface; this adapter does not call it.

Inside that admitted command, a synchronous SQLite transaction commits the
actual Message-ID, immutable origin and one-shot SMTP intent before entering
transport. No manual outbound registration is involved. SMTP success, partial
recipient rejection and Sent-copy failure are separate outcomes. An exception,
timeout or process death after intent is held/unknown; neither repeated
confirmation nor repeated preparation with that operation ID sends again.
A changed payload or origin with the same operation ID is rejected. Do not
create a new operation ID to work around uncertainty.

Supported initial surface: plain text, up to 20 exact addresses, no CC, BCC or
attachments. The old shell sender remains legacy/unbound and is NOT silently
claimed to be fixed. Switch the invoking workflow to `mail_prepare` + the human
confirmation; do not wrap the old helper with session-key arguments. If automatic
sending under a previous conversational authorization (without this explicit
confirmation) is mandatory, that is **not implemented**; the host needs a genuine
per-send authorization capability for this plugin. Never replace it with an
owner bit or an inferred consent string.

## Persistence, custody and inbound behavior

The adapter reuses the existing private mail SQLite DB (must exist, mode 0600,
owned by the service OS account, non-symlink path, parent not group/world
writable). It adds `mail_outbound_v1`; the router adds
`mail_origin_deliveries_v1`. Existing tables and routes are untouched.
SQLite IMMEDIATE transactions serialize outbound admission; the router retains
its existing runtime.lock for ingress/dispatch. No SMTP/RPC runs in a database
transaction and no lock file is replaced. Source installation, OS account and
operator-owned state are trusted, as for the existing route map. Mode 0600 is
**not protection from another process with the same UID**. This design does not
authenticate arbitrary SQL rows or resist a model with unrestricted code/exec
write access to the trusted installed code or DB. Verify current confinement/
custody before rollout; changing access rules is outside this patch. If those
files are untrusted/model-writable in the actual threat model, rollout is blocked:
do not label them attested or add a caller-held signing secret as a workaround.

Only validated In-Reply-To/References matching committed outbound IDs select
this lane. The sender's From and other headers never grant authority. Multiple
origins/incarnations, outgoing-ID reuse, conflicts with operator routes and
unconfirmed/definitely-unsent IDs remain triage. Missing matches use the existing
routing/fallback unchanged. A spoofed exact known ID can at most supply untrusted
report-only context to that already-bound audience; email authentication is NOT
claimed.

New replies use the existing router dispatch and Gateway `chat.inject` with
mandatory sessionKey + agentId + sessionId. The Gateway applies its existing
access checks and atomic incarnation fence. There is no classifier, agent wake,
task execution, Telegram notification, child fanout or fallback to a wider
audience for this exact-origin lane. Archived/reset/inaccessible sessions and
ambiguous delivery remain held, never retried blindly. Paused tasks are not
resumed. This lane sends a bounded preview with explicit incomplete-reading and
report-only warnings, not an LLM-generated summary or attachment attestation.
Existing content authorization does not automatically expand to these new
NULL-route rows; full-content access needs its existing separately authorized
workflow. No historical reply is retroactively admitted.

## Offline tests

```sh
python3 scripts/check.py
node --check adapters/mail-origin-plugin/index.mjs
# Use the already-installed serving source and its compatible toolchain.
# HOST_SOURCE and scratch must be operator-selected; no installs or core edits.
mkdir -p .local
MAIL_TEST_SCRATCH="$PWD/.local" MAIL_TEST_HOST_ROOT="$HOST_SOURCE" \
  node --max-old-space-size=384 --import "$HOST_SOURCE/scripts/tsx.mjs" \
  adapters/tests/mail-origin-runtime.test.mjs
```

The runtime test imports the real registry, v2 context construction, tool callback
binding and native command executor from that host source. Only the host input
and transport/RPC effects are synthetic. It proves a real adapter-created row
is visible to the existing Python router while SMTP is still in flight, then
checks exact incarnation delivery. It does NOT manually register origin and
does NOT install a plugin or prove connected-channel command delivery. Network
is disabled; fixtures and credentials are invented. The separate stdlib suite
forbids network, subprocesses and external files. A live canary remains required.

## Acceptance-gated rollout (operator only)

Do not copy this entire checkout over a deployment: deployed/private versions
may contain additional fixes or different imports. Freeze the accepted feature
SHA and verify it against the remote. This source branch does not include
unreviewed commits from another local branch.

1. Record deployment paths, service/scheduler entrypoints, current source hashes,
   DB path and owner, current lock path, SDK source revision and installed Node/
   Python versions. Compare each candidate import and changed function against
   live. Use hashes and reviewed diffs locally, never publish private source or
   route/config data. Resolve live-only drift explicitly; no wholesale replacement.
2. Run the two offline gates above in a disposable private directory on the
   deployment filesystem under a task-scoped 1 GiB memory cap. Confirm Node has
   `node:sqlite`, Python is 3.12+, real host command contexts expose owner-current
   checks and session incarnation, and the existing Gateway supports atomic
   `chat.inject.sessionId`. Older/unsupported hosts must remain disabled.
3. Verify state/source custody under the **current** model tool policy. A mere
   Unix owner check or config Boolean does not establish protection from that
   model's unrestricted exec. If a separate principal, new ACL or privileged
   adapter is needed, stop and request that precise separately scoped operation.
4. Stop new sender invocations using the operator's existing workflow pause,
   wait for running sends to settle, and hold the existing mail runtime.lock
   during the mail-only installation/migration window. No Gateway restart or
   policy mutation is authorized by this source change.
5. With the lock held, create an online SQLite backup (preserves WAL), record
   source checksums and route/config hashes. Example operator commands, using
   verified private variables, **not inferred paths**:

   ```sh
   umask 077
   python3 - "$DB" "$BACKUP" <<'PY'
   import sqlite3, sys
   with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
       source.backup(target)
       assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
   PY
   sha256sum "$DB" "$ROUTES" "$LIVE_ROUTER" > "$PRIVATE_PROOF/before.sha256"
   git -C "$CANDIDATE" rev-parse HEAD
   ```

   Backups/proofs are private and not committed. Additive schema creation is
   idempotent; inspect with `PRAGMA table_info(mail_outbound_v1)` and
   `PRAGMA table_info(mail_origin_deliveries_v1)` after first use.
6. Install the reviewed mail-only source/import mapping atomically. The native
   plugin entry is `adapters/mail-origin-plugin/index.mjs`; its manifest declares
   only `mail_prepare` and a human command. Operator config supplies absolute
   `dbPath`, `credentialsPath`, `pythonPath`. Credentials use the legacy
   three-key env format, supplied by a private owner-only non-symlink file.
   Never put credentials or mailbox values in plugin source or arguments.
   Preserve the checkout-relative worker path: a copied nested plugin directory
   alone is incomplete. The documented local managed-link operation is below,
   **only after separately authorizing installation/config changes**:

   ```sh
   openclaw plugins install --link "$CANDIDATE/adapters/mail-origin-plugin"
   ```

   Keep that accepted checkout immutable and retained. Do not install a detached
   copy of only the nested plugin folder. Plugin installation/activation and
   exposing the tool in existing policies require MAIN/operator action. If activation needs Gateway config/reload/
   restart or a policy change, stop and obtain that separate scope; this task
   does not authorize it.
7. Verify the real outbound invoking workflow now offers the complete draft
   and native human confirmation in its originating session. Run a no-delivery
   smoke first: prepare a synthetic draft addressed only to an operator-controlled
   sink, do **not confirm**, and verify status=draft and zero SMTP calls.
   A foreign session, reset incarnation, non-owner or denied invocation must
   leave it unsent. Never send to a third party as a canary.
8. For a complete no-real-mail canary, bind an isolated synthetic mail DB and
   transport sink in a test-only installed instance (not the production mailbox),
   exercise the real connected human command path, inject a synthetic reply
   through the existing router, and verify one exact sessionId-fenced passive
   context receipt and zero SMTP network calls. Do not add a configurable
   production bypass/fake authorization flag. If the host cannot isolate that
   instance without new permissions/configuration, report the missing operation.
9. Resume mail-only processing after custody, drift and canary acceptance. Keep
   the legacy sender unavailable in the documented originating workflow unless
   explicitly used as unbound legacy. No retroactive registration or replay.

## Rollback and operational reconciliation

Pause only the new invoking workflow first; inventory draft/inflight/unknown and
accepted rows using private read-only SQLite queries. Restore previous mail
source/imports atomically under runtime.lock; leave additive tables and ledger
rows intact. The old router ignores these new rows; do NOT reclassify them or
clear cursors/effects to obtain a retry. Never restore an old DB over new receipts,
delete the new tables, reset an inflight operation, or resend because Sent append
failed. A backup restore is disaster recovery, not routine rollback.

After rollback, compare route/config hashes, cursor and existing receipts with
pre-rollout evidence and run SQLite integrity_check. No Gateway restart, schema
destruction, scheduler change, external mail or historical processing is part of
these instructions. Unknown SMTP and unknown context ACKs require independent
positive evidence and an operator decision; the patch deliberately has no
automatic resend/replay or Sent-only retry command.
