# Delivery, content and recovery

These commands describe optional operator integration, not actions performed by
source extraction or tests. Use a dedicated ignored state directory and verified
operator routes. No job, daemon or scheduler is installed by this repository.

## Summarization

`tick` admits a bounded snapshot transactionally and emits a `batch` for an
external summarizer when `fire` is true. At most six messages enter a batch,
with bounded previews and two recent summaries per topic. Persisted originals
and deferred locators remain available. The emitted EVENT is untrusted context.

The external summarizer submits one result per topic using
`complete --batch BATCH_ID --result-json JSON`. Each result has only
`route_id`, `kind` (`ack`, `result`, `blocker`, `decision`), `summary`,
`details` and `next`. The JSON is an array of objects. Text fields must be
nonempty strings: summary ≤500, details ≤1100, next ≤300 characters. Extra fields
are rejected. Rendered Telegram text also has a UTF-16 budget check.
Complete is atomic and does not send. The first valid result wins under a SQLite
write transaction: its text, source keys, frozen routes and delivery effects are
immutable. ACK creates context effects only; results/blockers/decisions can notify.

A successful CLI response keeps `status=stored` for existing callers and adds
`completion_status=stored`, `terminal=true`, `next_action=NO_REPLY` and an
`accepted_result_digest`. The summarizer must immediately end its turn with
exactly `NO_REPLY`: no second complete, new draft, delivery call or user summary.
A helper response is an explicit stop signal, not an enforcement mechanism for
model turns; the integrating scheduler must include this rule in its payload.

Retrying the same batch with the same topic IDs and per-topic `kind` returns
`status=stored`, `completion_status=already_completed`, the same terminal/stop
signal and the first result's digest. It never overwrites a summary, adds delivery effects or consumes a newer
batch. Topic order is irrelevant. `replay=identical` means identical per-topic
objects; `replay=text_changed` explicitly means the new text was **ignored**.
This is first-writer-wins, not a claim that different prose is semantically equal.
Corrections require a separate operator workflow; complete is not an edit API.

A changed topic set or `kind` is a true structural conflict and still fails with
`batch_already_completed_differently`, retaining the original result and recording
the error. Malformed objects, duplicate topics, extra fields, invalid kinds and
out-of-bounds text still fail validation, including on retries. Do not change
classification or routes to bypass a conflict. Prose-only contradictions cannot be
reliably detected here; the winning result remains authoritative and the ignored
candidate is explicitly reported instead of silently replacing it.

Concurrent complete CLIs wait for the existing runtime lock within the bounded command deadline; other commands retain their nonblocking lock policy. Independent
Store connections serialize the first-result check and commit. Database contention
beyond the storage timeout/deadline is still a real operational error.

Seen readiness is independent of summary commit: a later readiness failure is
retained as `mail-seen:seen_queue_failed`, with `seen_queue=deferred` on the terminal
response, not a false failure of the already stored summary. Retrying completion
may recheck idempotent Seen admission after an interrupted post-commit step; it
never applies Seen or sends routing effects. Existing read-proof rules still apply.

Invalid completion text returns `summary_text_invalid` with a bounded `validation`
object: topic index, field, actual length, minimum/maximum and a fixed reason.
Malformed JSON returns `summary_json_invalid` with line/column/position and a
serializer/escaping hint; invalid base64/UTF-8 returns `summary_encoding_invalid`.
A literal control character is reported as `invalid_control_character`; JSON remains
strict (no sanitization or permissive parse). For an existing pending batch only,
JSON/encoding rejection returns `retryable=true`, `batch_retained=true` and
`next_action=correct_result_only` without creating an operational error alert.
Correct the input using a JSON serializer, then retry the same batch. An abandoned
batch remains visible as `summary_pending` to the watchdog. Missing/completed batch
validation, completion conflicts, storage failures and delivery errors retain the
operational error path. Existing sent alerts and receipts are never rewritten.
Generate JSON with `json.dumps` and pass it as an argv element to the helper using
`subprocess.run([...], check=True)`; do not hand-assemble JSON inside shell quotes.

No candidate text, decoder excerpt or exception payload is echoed or journaled.
These remain errors (exit 1), including after an earlier successful completion.

`tick --effects` may service admitted delivery, Seen and error effects within
its deadline. An operational routing-delivery error is durably retained and
reported in `dispatch`, with top-level `status=degraded` (exit 0). The admitted
`fire`, `batch` and authoritative `state.cursor` are still returned; callers must
process them even when delivery is degraded. This is not a successful-delivery
claim. Polling, storage, deadline and unexpected runtime failures remain errors.
Gateway calls retain a 7-second RPC ceiling and 10-second process ceiling.
The scheduled headless trigger gives one tick a hard 30-second wall clock
(`HEADLESS_TRIGGER_WALL_CLOCK_MS`). The helper therefore bounds itself strictly
inside that window: an 18-second in-process alarm, a 15-second snapshot poll,
delivery shared until second 15 and disposal/error dispatch until second 13,
leaving margin for process spawn, result projection, Code Mode resume/teardown
and status publication (~9 seconds observed after the child exits).
The scheduler-side trigger exec keeps a `timeoutSeconds=19` backstop just above
the alarm, so the helper normally has time to return its durable fail-closed JSON before the
exec backstop. Host stalls can still prevent timely settlement. With the former
27-second alarm and 28-second exec timeout, a slow evaluation was observed to
hit the wall clock and be cancelled mid-flight (`failurePhase=bridge`,
`failureCode=tool_error`); that observation does not establish historical batch
or cursor loss. The ordering (alarm < exec timeout < wall clock,
with at least ~9 seconds of post-child overhead reserved) is pinned by
`test_agent_mail_delivery_progress.TriggerBudgetInvariantTests`. All five bounds
are overridable for a task-scoped re-tune via `MAIL_ROUTER_TICK_ALARM_SECONDS`,
`MAIL_ROUTER_POLL_TIMEOUT_SECONDS`, `MAIL_ROUTER_TICK_DELIVERY_SECONDS`,
`MAIL_ROUTER_TICK_DISPOSE_SECONDS` and `MAIL_ROUTER_TICK_SEEN_SECONDS`; the
alarm must stay below the exec timeout, which must stay below the trigger wall
clock.
Validation
gets at most half the remaining budget so the delivery call has time left;
process/RPC timeouts are shortened accordingly. Budget exhaustion before intent
leaves pending, not unknown; uncertain post-intent results remain held. Telegram
still requires its existing three-second transport budget before intent. At most
ten routing steps run per tick. A slow successful poll or first context no longer
silently excludes all delivery just because elapsed time exceeds six seconds.
FIFO summary order prevents new contexts from starving an older notify, while
contexts within each summary remain first. Bounded time/call counts are not a
liveness guarantee under persistent poll/transport failures or saturated backlog. `dispatch` services one routing effect without IMAP or Seen.
`errors-dispatch` services only the independent mail error outbox.

## Read-only state inspection

```sh
python3 tools/agent_mail_router.py status
python3 tools/agent_mail_router.py errors-status
python3 tools/agent_mail_router.py original --key VALIDITY:UID
```

SQLite ingress and lifecycle ledgers use WAL/FULL. Preserve databases, WAL and
receipts together when backing up with a consistent SQLite backup mechanism.
A cursor may advance once the full fetched batch is durably admitted, before
outbound delivery; that is not proof of user receipt.

## Delivery invariants

Thread Message-ID identity wins over task references, then general fallback if
explicitly configured and safe. Generic topic words do not establish identity.
Unknown senders cannot acquire anchors or execution/diagnostic capabilities.
New external mail can enter report-only batches: verified operator-registered
outgoing IDs select an origin, otherwise only the configured general fallback
is eligible. Task keywords alone do not select a task for external mail.
Changing routes cannot reroute already classified/delivered summaries.

Context is validated/serviced before notification, detecting an absent technical
child before the Telegram send. A complete successful listing proving absent or
archived parent/child can freeze an operator general destination in the additive
`delivery_fallbacks` table. Timeout, incomplete listing, lineage mismatch and lost
ACK do not prove absence. Original summaries, identity, mail and cursor remain
unchanged. Multiple unavailable contexts still create at most one notification.

Notifications run only from 10:00 through 23:59 in Asia/Tbilisi, including the last
transport-time gate. Context can run at night. Available-route targets and receipts
are unchanged by fallback. A notification that is sent, inflight, unknown or
rejected is not copied to general. An external task report is held when its exact
destination is unavailable; destination failure cannot widen its audience.

Telegram has no idempotent sendMessage. An uncertain ACK is held `unknown`;
interrupted routing intent becomes unknown on Store reopen. Neither is blindly
replayed. Rate-limit rejection remains bounded pending; definite rejection is
held. Positive context-marker evidence may reconcile a context; bounded marker
absence never permits retry. Reconciliation requires a Gateway-injected assistant
message in the exact target session, an exact marker line and a real persisted
message ID (including the current `__openclaw.id` history shape). A quoted user
marker, an ordinary model response or a marker without a receipt is not proof. Independent topics continue.

After independently proving receipt or definitive non-delivery, operator commands
`resolve`, `resolve-error` and `resolve-legacy` accept evidence. Read their CLI
help; do not invent receipt strings or delete rows to force retry.

## Deferred originals, documents and Seen

`agent_mail_content.py` provides `prepare`, `status`, `handoff`,
`accept`, `read` and `seen`. Prepare and Seen require explicit individual keys;
no implicit historical backfill. Acquisition uses EXAMINE/BODY.PEEK and does not
mark mail read. Exact runtime-admitted report-only rows permit preparation and
Seen eligibility checks without --include-unknown; the stored row and operator
configuration, not a mail-provided policy flag, establish this reading boundary.
Other unknown-source preparation still requires explicit review and never
promotes sender trust. Seen application still requires full read proof plus an
accepted summary; reporting or screening alone cannot establish either.

Limits include 64 MiB EML, 24 MiB decoded part, 16 MiB text per part, 256 MIME
nodes, 24,000 characters per read, 1 GiB physical content store, 180-second worker
deadline and 768 MiB address-space ceiling. Unsupported/limited/partial content
is reported honestly. No remote resources, scripts, archive extraction, attachment
execution or SMTP is performed.

Document/OCR parts emit `MAIL_TECH_DOCS_CLEAN_LAYER_V1` handoffs. An external
read/extract-only specialist must return hashed text, sections, page maps and
format/quality evidence under the assigned output directory. Accept validates
source hashes, path confinement and outputs; read rechecks integrity. No automatic
specialist invocation is bundled. Extraction availability is not proof of model
reading.

Seen requires an accepted summary and complete SHA-bound range evidence for all
clean readable parts (or the narrow verified legacy single-body equivalence).
`seen --key VALIDITY:UID` inspects eligibility; adding `--apply` performs exact
`UID STORE +FLAGS.SILENT (\\Seen)` and verifies UIDVALIDITY, UID and flags. Existing
flags are preserved; no expunge or blanket marking. A lost Seen ACK may be retried
after proof/flag revalidation because adding this flag is idempotent.

## Error outbox and rollback

Mail poll/content/integrity/Seen/delivery errors have fixed secret-free codes.
The independent ledger deduplicates fingerprints; a fsynced append-only fallback
journal is replayed after SQLite recovery. Error notifications use the separately
authorized error topic and the same quiet-hours/unknown-ACK policy. This is not
a host-wide watchdog; failure before Python starts is outside its observation.

Rollback code independently of state: retain ingress DB, lifecycle ledger, spool,
cursor, anchors and delivery receipts. Never overwrite a live ledger with an old
snapshot or remove receipts to replay mail. Reverting Seen requires a separate
exact-UID operator decision. No deployment rollback is performed by this repo.

## Code Mode exec-result adapter

`scripts/mail-trigger-result.js` is the result-validation fragment inserted after
an existing trigger's `exec` call and before its `let event` JSON validation.
It does not replace the command, trigger state, summarizer instructions, or models.
Code Mode projects exec `details` directly: a completed command still requires
an integer `exitCode` of zero. A textual JSON event without exit evidence is not
success. Nonzero exit, explicit process failure, running, missing exit evidence,
and overall/no-output timeout are distinct fail-closed outcomes. A timeout does
not prove that ingress/outbox effects did not happen; reconcile durable state
before any repeat. Do not reset cursor, unknown effects or delivery receipts.

Run `node scripts/test-mail-trigger-result.mjs` for adapter regression coverage.
Prepare a trigger-only scheduler patch with the original script hash and preserve
its entire instruction suffix byte-for-byte; only the scheduler owner applies it.
A direct shell tick is not a Code Mode execution proof, and an isolated headless
Code Mode proof does not establish scheduled admission or cron deadline success.

For observation without paid analysis, use the deployed watchdog `check` command,
not `check-analyze`. Resolve `--scheduler-db` and `--store-key` from the actual
cron row; the latter is a stored identity and need not name an existing JSON file.
Prove deployed sibling imports and run `check` against scratch mail/lifecycle
copies before preparing activation. Keep live repair/analysis admission unchanged.

## Bounded completion and dispatch

`complete` uses a 12-second internal alarm. A committed batch is authoritative even
if independent Seen admission fails. After an interrupted or nonzero completion,
query `batch-status --batch BATCH_ID`: `completed` includes the accepted result
digest and is terminal; `pending` permits a controlled retry of the same result.
The status query opens SQLite read-only and does not recover or send effects.
Never infer delivery from completion or retry an unknown delivery.

Watchdog repair passes a monotonic absolute deadline inside its process timeout;
dispatch reserves time to persist and return. A read-only validation timeout or
insufficient pre-intent budget defers without sending. After durable send intent,
an ambiguous outcome remains unknown until positive reconciliation. These limits
do not fix independent Gateway storage or scheduler delays.

## Source and deployment mapping

This tree combines the portable external report-only intake with optional work,
watchdog and repair source. The portable `tools/agent_mail_*.py` names and
`agent_mail_settings.py` configuration are not byte-identical to an installation
using `syncopia_agent_mail_*.py` and deployment-specific imports or settings.
Review the complete installed/portable diff and an explicit module mapping before
any deployment; do not copy example policy or overwrite state. No runtime policy,
route, ledger, cursor or receipt is included in the source manifest.

A summarizer integration must constrain its runner to prepare, read, intake,
complete and batch-status operations. After an uncertain completion it queries
the same batch ID and stops on a committed result; it does not launch free-form
workspace diagnostics or infer delivery. This repository supplies the status
operation but does not change or enforce an existing scheduler's instructions.
