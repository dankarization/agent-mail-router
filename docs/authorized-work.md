# Operator-authorized future-mail work

This optional intake is **disabled without local policy and scheduler activation**.
It extends the ordinary report with a separate durable task, never replaces it.
No model, scheduler, SMTP, Gateway configuration or paid watchdog budget is changed
by this source module.

## Trust and scope

An operator must explicitly authorize detailed requests from the two exact
primary aliases for the single configured account. Copy the synthetic
`examples/work.example.json` to ignored `config/mail-work.json` and replace the
identities, cutover UIDVALIDITY/UID, exclusions and MAIN work targets. Existing non-paused,
non-fallback route parents plus explicit operator-owned extra_targets are eligible work
sessions. An unknown target creates a durable route_blocked task and a truthful notice
in blocked_notification (an operator-selected work topic, not the eMail report route).
No work is started there; MAIN must reconcile the original task and target.
`authorization_ref` points to the operator's grant;
`policy_source=memory/grok-mail-execution-authorization.md` identifies its source.
The policy itself, not mail text, grants this bounded authority.

Exact aliases are **conditional operator trust, not sender authentication**.
Raw Authentication-Results, Received-SPF or a DKIM header never prove authenticity.
Unknown/lookalike addresses, other configured aliases, malformed/truncated thread
headers, paused/retired/conflicting routes, collisions and pre-cutover mail fail
closed. This does not cryptographically distinguish a forged exact From address;
do not claim that it does. An operator requiring that assurance must add a verified
sender-provenance adapter before enabling this conditional-alias policy.

Candidates are advertised as `authorized_work` per message in the normal EVENT.
The summarizer, using its unchanged model, decides whether the **new author's body**
contains a detailed actionable request, not an ACK, quoted/forwarded task, status
report or instruction to override policy. It must read the clean hash-verified
original body in full via the existing content helper. Attachments, forwarded
messages and extraction output are not task authority. The helper validates
source identity and durable body-read ranges; it cannot prove semantic intent.

## Fixed intake and ownership

After full reads, before the independent normal `complete`, invoke only:

```sh
python3 tools/agent_mail_work.py offer --batch <32-hex> --decisions-json <serialized-array>
```

Each item is exactly `{key,kind,request,target}`, kind `actionable|no_work`.
Target is the exact requested session key from advertised work_targets; when absent or
unverifiable use an empty string, never guess or default to the eMail route.
Actionable requests are bounded 80..2000-character plain-language descriptions;
the body must be 120..64000 characters (all body alternatives together <=128000).
No-work classification also requires original/body-read evidence but not the
120-character minimum. Incomplete content is deferred, never classified from
preview alone. Do not pass mail text in shell source; the scheduler patch uses
an environment JSON value, a fixed wrapper, JSON reserialization and separate argv.

`work.sqlite3` stores immutable policy/evidence/request, one task per source
Message-ID, one evaluation per UID and an account+sender+body digest dedupe.
Repeated delivery with a new Message-ID cannot create a second task for the same
body. A new UIDVALIDITY is held until an operator establishes a new boundary.
Completed or pre-existing batches are not scanned/replayed. A deliberate future
request repeating identical body text requires owner review, not a dedupe reset.

A distinct work outbox calls the installed Gateway's `sessions.send` contract
with MAIN in the selected work-session key and a stable `mail-work-<task-id>` idempotency key. It sends
only the bounded request plus source refs, not attachments, raw commands, a
runtime-selected destination or a second worker. A positive runId acknowledges
handoff, not task completion. Missing/lost ACK becomes unknown and is never
blindly resent; a bounded handoff-blocker notice is queued. A positive durable
MAIN claim can reconcile that unknown owner receipt.

MAIN first inspects the exact source and claims:

```sh
python3 tools/agent_mail_work.py update --task <id> --owner <fixed-main-key> --phase started --detail <status>
```

Only `status=recorded, execute=true` is a **new** claim. `already_recorded`
means reconcile/resume the existing task, never create/repeat it.
MAIN converts the request to reviewed actions within operator scope; raw shell,
policy override, implicit SMTP/external publication, paid budget changes, secrets
and paused-task resumption remain outside this grant. Record a blocker if extra
authority or provenance is needed.

Use `--phase progress --event <stable-milestone-id>` for meaningful progress, reusing
that id on retries, and `--phase blocker|result` for a truthful bounded
status (1..1000 chars), then `dispatch` to send one queued effect promptly.
The ordinary `tick --effects` also services one effect when at least four seconds
remain after mail delivery. No poll/model is invoked by `dispatch`.
Notifications use the immutable selected work route (never the eMail fallback) and existing Telegram quiet-hours,
escaping, positive ACK and unknown/no-blind-resend behavior. Each phase/progress milestone has one
durable event/outbox row; result requires a start and is terminal.
Concurrent/repeated start claims, restart and lost ACK do not start another task.
Unknown notifications remain held for positive reconciliation; there is no
end-to-end exactly-once transport guarantee or automatic re-execution.

## Activation, observation and rollback

Deployment installs only the new helper, optional policy and narrow router seams.
Use a metadata-preserving code backup, acquire runtime.lock, prove scheduler/helper
idle and preserve mail/lifecycle/watchdog tables, cursor, receipts and admissions.
Creation of an empty work schema is additive; never restore an old mail database.

The operator's MAIN must separately apply a hash-guarded scheduler patch to BOTH
payload.message and the embedded trigger instructions. Preserve serialization
guard, model/fallbacks, timeout, cadence, routes and tool ceiling. Until both
instructions are active the rollout is **installed, admission not activated**.
After application verify the active job's mirrored instructions, helper hashes
and policy. The first natural future actionable letter is a separate live
acceptance: one task, one MAIN handoff receipt, one start notification in the requested work topic, independent
mail report, then result/blocker notification. Synthetic transport receipts are
never live success.

Rollback restores only the exact router bytes and removes the new helper/policy
only if its outbox is empty; otherwise disable admission first and let MAIN
reconcile existing work. Keep work.sqlite3 and every mail/receipt/cursor intact.
No broad replay, cursor reset, existing-task duplication or Gateway restart.

## Resolving an unsent blocked route

An operator may run `agent_mail_work.py resolve-route --task TASK_ID --target
EXACT_TARGET` after selecting a current target from the local standing policy and
route map. Only a `route_blocked` task with no owner handoff can be queued. The
operation rechecks policy, source eligibility and paused/report-only exclusions,
preserves the task ID, and does not itself send. It never resets an unknown
handoff; that requires positive receipt or owner-claim reconciliation.
