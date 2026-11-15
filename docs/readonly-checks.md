# Mail-triggered read-only checks

This optional branch is separate from paid mail-service incident analysis.
The existing mail summarizer primary/fallbacks remain unchanged. The trigger
continues to poll/admit mail and deliver existing outbox effects without checks
or model work on quiet ticks.

## Operator boundary

Copy `examples/readonly.example.json` into ignored `config/mail-readonly.json`.
Only the two exact operator-owned conditional sender aliases are eligible. A
parsed From is **not authentication**; mail remains untrusted data. Targets are
fixed `hp.tcp` and `vm140.tcp`, each with an operator-proven numeric address,
port 22 and provenance, or explicit null for an unresolved mapping. No target,
URL, command, path, credential, permission or code is taken from mail/model.
A null mapping produces `mapping_unknown` without a network request. Operator
policy is never writable through the model interface.

`run --batch <32hex>` opens the pending batch and original mail database read-only.
It never constructs the routing Store, recovers receipts or changes cursor,
routes, paused tasks, Seen or existing effects. The separate `readonly.sqlite3`
contains only selection intent, per-mail claims and fixed check metadata.
A durable intent is committed **before** the model request and before each TCP
connect. Crash/inflight means unknown, not retry or healthy. Dedup is by mail key
and check ID, with one selector attempt per mail/batch across process restarts.
Completed/unknown/failed attempts are not retried automatically.

## Model and dispatcher

The selector makes one tool-free OpenAI-compatible request to the operator's
existing local OmniRoute endpoint, using the existing credential in place (not
copied). Fixed model `nr-mimo-flash`, max_tokens 160, no client fallback or retry.
Endpoint must be numeric loopback HTTP; proxies and redirects are disabled.
The model sees bounded untrusted mail and an exact candidate list; it may select
at most two candidate `{key,check_id}` objects. Extra fields, unknown IDs, keys,
commands, duplicates, excess checks or malformed output fail closed.
Selection is a recommendation under operator policy, never new authority.
No Sol/High analyzer, agent exec, shell, SSH auth or remote command is involved.
The local provider's upstream identity/own route behavior is not proven just by
requesting an alias; distinguish fixture, requested route and live response.

Outside the trigger the existing summarizer may call exactly:

```
python3 tools/agent_mail_readonly.py run --batch <batch_id>
```

Only for a new meaningful availability blocker with advertised candidates,
not for ACK/repeated status. The CLI deadline is 12 seconds; selector socket
budget 6 seconds, at most two single TCP connects of at most 2 seconds each.
An integrating exec timeout of 14 seconds bounds helper termination overhead.
No tests from mail, wake/restart/install/fix, replies, GitHub writes or resumed
paused tasks. A TCP/22 connect without bytes is **not** host identity, health,
test readiness or evidence from the sender's network vantage point.

## Reporting and failure isolation

Always complete the ordinary mail report, regardless of selector/check status,
invalid policy, timeout or insufficient remaining budget. Invoke checks at most
once and leave at least 30 seconds of the existing summarizer budget; otherwise
skip checks and report unknown. Never delay complete waiting for diagnostics.
Batch admission, completion and render do not invoke models or network checks.
Completion snapshots a bounded report into the existing summary; render appends
it only within the existing Telegram size budget. Failure is never healthy.
The existing context/notify outbox, positive receipt reconciliation, ACK policy
and quiet-hours gate handle the single report; check status never promotes ACK
to notify. There are no detached workers or late supplements: timed-out results
are not polled, and old digests are not resent.

## Rollout and rollback

MAIN owns scheduler activation. Update both payload.message and the embedded
trigger instruction literal from the same text; preserve model/fallbacks,
trigger state, cursor, schedule and delivery configuration. Deploy the new
helper, adapted router and operator policy with hashes/rollback under the
existing runtime lock. Preserve all runtime-only identity/route adaptations.
Tests must isolate network/model boundaries and prove both canonical and
adapted bytes. One separately authorized live smoke must name time/vantage;
never label a fixture as live model selection. Removing scheduler instructions
and restoring only changed helper bytes rolls back behavior; keep additive
readonly state to retain no-retry claims. Do not restore old database snapshots.

Before a direct `run` creates diagnostic state or calls the selector, it resolves
each persisted mail route against the current operator-owned `state-dir/routes.json`,
the same route map used by work admission. Missing, invalid, retired and
`external_report_only` routes are ineligible; the EVENT marker or message From
is not route authority. Keep router `--routes` aligned with this state-dir map.
This is a policy snapshot for the run, not an atomic lock against later operator edits.
