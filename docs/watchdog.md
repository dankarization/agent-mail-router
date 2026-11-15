# Mail-only incident watchdog

The observer reads only the exact configured mail scheduler row and bounded
metadata projections of mail/lifecycle outboxes, Seen intents and summary batches.
It never changes mail, cursor, summaries, receipts or scheduler definitions.
A successful initial snapshot baselines historical error counters without replay.
New candidates must persist 300 seconds; recurrence needs 900 seconds of observed
health. Source uncertainty breaks recovery; poll success cannot heal content faults.
A stored lifecycle error counter that advances for an occurrence whose own last
occurrence is older than the 300-second stability window is treated as already
covered (baseline, previous observation, or the observer's own blind gap across a
restart) and does not open a `runtime_failure` incident; a genuinely recent
occurrence still does. Missing freshness metadata fails closed. This prevents a
stale alarm from being re-admitted as a new generation and re-delivered.
Pending delivery is not itself a failed send. The observer durably tracks a
bounded metadata-only clock per eligible pending effect in `wd_meta`. One full
mail-service interval (900 seconds in the configured deployment) must be observed
before it becomes overdue; the existing 300-second stability gate then applies.
A pending notify is eligible only in daytime and after its own pending contexts
are serviced. Quiet hours, disappeared/ineligible effects, invalid snapshots,
backward clocks and observation gaps longer than one service interval reset this
clock. No mail schema, cursor or delivery receipt is changed. Held unknown,
inflight and rejected delivery outcomes remain immediate failure signals and are
never released by this grace period. This is a service-lag signal, not proof of
a transport failure or a guarantee that arbitrary backlog fits one tick.

## Explicit paid-analysis policy

`enable-analysis` activates paid_analysis_v2. Explicit `--incident` IDs migrate
only open `hard_cap_unavailable` incidents, preserving their entire old rows in
`wd_policy_audit`; old decisions/reservations in `wd_days` remain unchanged.
Explicitly named, still-open legacy incidents may be diagnosed during recovery;
closed and other blocked history is never replayed. Deployment alone does not
activate this policy or change a schedule.

New sustained incidents become analysis_ready. A separate BEGIN IMMEDIATE
transaction inserts a unique `wd_analysis` row and marks the incident admitted
**before** starting the consumer. Maximum one analysis per incident and two
conservative paid admissions per Asia/Tbilisi calendar day, across concurrent
processes. Failures, crashes, startup failures and ambiguous outcomes retain their
slot; no retry, midnight replay, fallback model or recursive analysis. A dead
inflight attempt becomes unknown after 90 seconds without reclaiming admission.

`wd_days.tokens` is legacy policy reservation, never billing. `actual_admissions`
counts conservative attempts, not proven HTTP calls or charges. `llm_calls`
counts completions with a verified model route; ambiguous attempts may have cost
money. `llm_tokens=null` means aggregate actual consumption is unknown. Per-run
reported usage is retained when supplied. **There is no exact total token cap.**

Consumer failures retain the preflight/dispatch stage, process-start flag,
exit status, elapsed time and bounded output byte counts/hashes; raw stderr,
stdout and exception strings are not persisted. A preflight failure starts no
agent process. A started agent process is not proof of a provider request.
The CLI envelope verifies the reported model only; it does not expose effective
thinking, so thinking_verified remains false even when --thinking high was
requested. Neither route health nor consumed admissions proves successful live
analysis. Interrupted historical attempts with discarded diagnostics remain
unverified; new code does not rewrite their rows or reclaim their admissions.

The explicitly selected model is openai/gpt-6.1-sol with thinking high and a
preferred operator-supplied OpenAI OAuth profile. An optional exact secondary
OpenAI profile may be selected by native models-status health before dispatch,
only if the primary is unavailable; the selected profile is then pinned for the
one turn. No post-dispatch profile failover or model fallback is configured. The installed `openclaw agent exec` runs one isolated headless turn:
no inherited model fallback chain, empty workspace, skipBootstrap, 16384 context
window, deny-all tools/native delegation, read-only native sandbox, no approvals,
at most 60-second requested runtime deadline. Native models-status health runs
for every route (including primary-only), explicitly selects the same main agent,
requires a usable Codex route and has a 30-second process deadline without
--probe or inference. Preflight and the single dispatch share an 85-second
consumer budget; dispatch receives the remaining whole seconds minus a
10-second process/cleanup margin, capped at 60 seconds. Process-group kill/reap
has at most five extra seconds. The caller's unchanged 100-second alarm also
covers metadata observation and completion publication; the 110-second scheduler
limit is operator-owned. Slow startup therefore reduces model time instead of
extending the outer deadlines.
Prompt input is at most 4096 UTF-8 bytes of incident identity/lane/timing/counters;
mail bodies, addresses, subjects, raw errors and credentials are not included.
There are zero analyzer tools (stricter than the earlier four-read-only ceiling).
Output is bounded at 65536 bytes and persisted text at 2400 characters. These
bounds are not a provider billing guarantee or a total-token hard cap.

The private analyzer config contains `profile`, optional `fallback_profile`, and
`openclaw_config` (the
read-only credential-routing source). It cannot supply a command, alternate
model, fallback, prompt or notification destination. Existing dependencies and
installed native runtime are reused; no Gateway config is written or restarted.

## Completion and notification

New error alerts keep the existing lifecycle error slot, quiet hours and held
unknown/inflight semantics. Admission is resolved before the check-analyze error alert is projected and sent,
then the admitted consumer runs. A crash after claim still spends that slot.
Exact enums distinguish ready (not admitted, no promised result), admitted (not
proof of a provider call/success), daily-blocked (no new admission), and terminal
ok/error/timeout/unknown outcomes. Unknown enums promise nothing. Only pending
error notes advance; sent/held history, receipts and attempts are never rewritten.
A plain check can still report readiness without starting or promising analysis. Analyzer errors are stored only in
wd_analysis and cannot feed observation recursively.

Completed analysis is atomically claimed for one `chat.inject` into the existing
MAIN mail error session. A positive MAIN message receipt is required before the
MAIN-owned single-call Telegram adapter may publish the result to that exact
mail topic. No extra MAIN model turn is started. Both MAIN context and Telegram
notification have independent durable claim/receipt slots. Pending is eligible;
inflight/unknown/rejected is held without blind resend. For held MAIN slots, a
matching persisted gateway-injected message may reconcile the positive receipt;
missing or failed history never permits another injection. Reconciliation does
not release an uncertain Telegram slot. Quiet hours remain
10:00–23:59 Asia/Tbilisi, checked again at transport. A lost ACK is not proof of
delivery; no end-to-end exactly-once claim is made. MAIN receives programming
escalation as text for a concrete handoff, never a new competing writer.

Bounded repair is a separate, explicitly activated allowlist, not general
auto-repair. It is off by default (`recovery=report_only`); after
`enable-repair --action NAME` it reports `recovery=bounded_repair` with the exact
enabled actions. Only a currently open, unhealed, classified incident whose
(lane, code) pair maps to an enabled action is eligible: at most one action per
tick, a durable attempt counter with backoff, a durable lease against concurrent
repair, and a fixed action timeout. Actions are idempotent and re-validated
independently by the executor helper: replay the local error journal, dispatch an
already-pending mail effect, or apply an already-decided Seen key. A pending
delegate is skipped while any inflight/unknown/rejected intent exists, so an
uncertain outcome is never blindly replayed.

Starting an action is not success (`outcome=attempted`). Only an independent,
later, ordinary healthy observation sets `recovered` with a verification time.
Unknown or failed outcomes are never retried automatically; incident history,
cursors, receipts, admissions and summaries are never modified by repair. Repair
never sends mail, rewrites receipts, resets a DB/cursor, edits code, deploys,
restarts a service, switches a model or reclaims an analysis admission, and it
never diagnoses: unsupported causes and code defects stay a MAIN handoff.

## CLI / activation ownership

```
python3 tools/agent_mail_watchdog.py check --state-dir STATE \
  --scheduler-db DB --job-id EXACT_MAIL_JOB --store-key EXACT_STORE
python3 tools/agent_mail_watchdog.py enable-analysis --state-dir STATE \
  --incident EXPLICIT_OPEN_PREVIOUSLY_BLOCKED_ID
python3 tools/agent_mail_watchdog.py check-analyze --state-dir STATE \
  --scheduler-db DB --job-id EXACT_MAIL_JOB --store-key EXACT_STORE \
  --analyzer-config PRIVATE_CONFIG --dispatch
python3 tools/agent_mail_watchdog.py enable-repair --state-dir STATE \
  --action replay_error_journal
python3 tools/agent_mail_watchdog.py repair --state-dir STATE
python3 tools/agent_mail_watchdog.py status --state-dir STATE
```

The observer never launches a process itself. Process launch lives in the
separate allowlisted executor helper `tools/agent_mail_repair.py`, which accepts
only exact argv shapes (`run` for one delegate, `cycle` for one ordinary check
tick plus at most one bounded repair, forwarding `runner` into the watchdog):

```
python3 tools/agent_mail_repair.py cycle --state-dir STATE \
  --scheduler-db DB --job-id EXACT_MAIL_JOB --store-key EXACT_STORE --dispatch
```

Without --dispatch no Telegram send occurs; an analyze command still publishes
its completion context to MAIN. A plain check never starts a model. MAIN updates
only the existing watcher command job to check-analyze and its command deadline;
keep the original mail job/trigger and cursor intact. No scheduler mutation is
performed here. Healthy/repeated checks start no model process.

Rollback: MAIN disables paid invocation (restore the old command or disable only
the watcher), then restores only hash-bound previous helpers. Retain all additive
analysis/policy state and both receipt slots; never restore an old DB or remove a
ledger to reclaim budget. Re-enable using the retained state. Host, scheduler,
Gateway or Telegram outages can prevent observation/completion/delivery; this is
not independent monitoring of those systems.
