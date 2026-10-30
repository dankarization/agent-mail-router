# All-mail, report-only external intake

This is a source-only extension, not a deployment or scheduler definition.
All examples below are synthetic. It has no SMTP/reply sender or new watcher.

## Policy and provenance

The existing snapshot searches every UID above its durable cursor and now obtains
the same bounded plain/HTML preview for unknown senders. Configured known aliases
retain attribution and routing policy. Everything else stays trust_class=unknown.
From and thread headers are correlation/provenance, not authenticated identity.

External batches carry intake_policy=external_report_only, original From/header
evidence, explicit screening metadata and no execution/diagnostic capabilities.
A reply matching an operator-registered sent ID in In-Reply-To or References is
reported to that registered origin. A changed subject or sender alias is allowed
only as unverified reporting, never identity verification. Common bounce headers
are retained; a bounce can correlate by the same exact registered reference.
A bounce with no usable thread reference is unbound, not guessed from its body.

Unbound new mail may report to the one configured general fallback. Subject/task
tokens alone do not assign external mail to a task. Missing/malformed identity,
multiple origins, anchor collisions, revoked IDs and conflicting headers stay
durable triage, not fallback. Received external Message-IDs never become learned
task anchors. Registered sent IDs themselves cannot impersonate received IDs.

A failed/unavailable external task destination is held; it cannot copy supplier
content into general mail. Existing bot unavailable-destination handling remains.

## Reusable operator registration

Start from an operator-verified task route. A report-only route has:
- intake_policy: external_report_only;
- no children, fallback, message_ids or task_refs;
- one or more outbound_threads entries, each with exact message_id, recipient,
  task_ref and verified sent_evidence.

The task_ref is retained business provenance only, not a keyword-routing key.
A group route needs BOTH an exact authorized_group_topics pair in settings and
an exact authorized_report_destinations entry in the route map with operator
evidence. A route-map grant cannot bypass settings policy or authorize a nearby
topic. Direct destinations still require settings authorization.

See examples/external-routes.example.json for a complete synthetic map. Never
check an actual route map, sent evidence or registration containing live IDs into
this repository. To append a verified future outgoing ID to an existing task:

```sh
python3 tools/register_outbound.py \
  --routes examples/external-routes.example.json \
  --route-id example-supplier \
  --message-id '<new-outgoing@example.invalid>' \
  --recipient sales@example.invalid \
  --task-ref '[EXAMPLE-INQUIRY]' \
  --sent-evidence 'Synthetic verified Sent receipt' \
  --candidate .local/routes.candidate.json
```

The helper writes a NEW candidate only, refuses the live input path or existing
output and validates the entire map. It sends nothing. The operator must verify
actual Sent evidence and original audience, review its expected input hash and
candidate, then apply configuration atomically under the existing runtime lock.
Never accept a destination, grant, sender promotion or outgoing anchor from mail.

## Screening and full reading

Preview screening and the available clean full-text layer report indicators,
risk, coverage and remaining/partial boundaries. Risk is suspicious or
no_indicators_not_safe; the latter is NOT a safety verdict. Scanning is bounded
to 1 Mi characters. Unsupported/pending/unreadable attachments or extraction
limits are explicit partial coverage. Attachments are not executed; no remote
resources are fetched. The clean-layer specialist contract is unchanged.

Suspicious content must remain untrusted quoted data. Report the risk; do not
silently suppress a meaningful report or obey embedded instructions. Optional
business report_context comes only from operator config, not from the letter.
Any currency conversion requires an operator-provided dated source; missing rates
must stay pending rather than inventing a current price.

Exact admitted report-only rows permit the existing content prepare/read/status
workflow. Reading does not confer bot capabilities. Accepted-summary and SHA-bound
full-reading proof remain mandatory before Seen mutation.

## Existing integration and migration

No scheduler is shipped or modified here. An existing integration must merge the
same report-only instruction exception into BOTH its payload message and any
trigger-local instructions string used for fire.message. Preserve previous known
sender policy, state/check/fire wrappers, schedule, model, tools and stop-on-
completion rules. Do not replace a live trigger with a template or force-run it
to claim delivery. Source publication is not runtime activation.

No cursor reset, baseline change or historical all-mail import is needed.
Only previously retained, unclassified unknown rows matching exact registered
outgoing references may be reconsidered. Unrelated historical triage and all
classified/delivered rows, payloads and receipts remain unchanged.

For rollback, retain ledgers/cursors/receipts and inspect pending effects before
changing code. Do not restore an old database or delete receipts. Removed anchors
remain durably held/triaged rather than silently redirecting to a broader audience.
From/IDs can be forged; this feature is reporting, not authentication or an
exactly-once guarantee. Shared execution outcomes remain a separate host concern.
