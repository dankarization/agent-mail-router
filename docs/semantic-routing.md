# Semantic routing of mail context

This optional feature copies the context of a newly received fallback email to
one eligible existing session. It does not send email, wake an agent, execute
instructions in a message, or assign new permissions. Exact reply, thread and
task routing keeps precedence. The existing general-mail report is unchanged.

A tick started with `--semantic-enqueue` records each eligible new message in
the existing SQLite `meta` table in the same transaction as the mail row and
cursor. There is no historical scan or database schema migration. The separate
`agent_mail_semantic_worker.py` command claims at most one entry per invocation;
a model call is not performed while the short tick holds its runtime lock. A
separate worker lock excludes overlapping workers. Unknown model or delivery
outcomes remain held, never blindly replayed. A crash can leave an entry in
`processing`; investigate it as unknown before any manual action.

The worker prepares an explicit mail key through the existing content helper,
using existing report-only policy. It verifies the original and extracted text
hashes and reads a complete, single preferred plain-text body (or HTML text if
plain text is absent). Missing, ambiguous, changed or oversized bodies are held.
Messages, headers, filenames and sender addresses do not grant work authority.

An operator-owned private scope limits the catalog to an explicit owner profile,
account, allowed agents and direct user. Group destinations require exact
operator grants. Archived, paused, child, background, cron and foreign sessions
are excluded. The model sees bounded untrusted mail text and up to 128 eligible
metadata descriptions with opaque IDs, subject to a 20 KiB serialized-catalog cap.
The letter body remains limited to 12 KiB. Private JSON transport permits 192 KiB
for the classifier envelope and 96 KiB for Gateway RPC, including JSON escaping;
these wire bounds do not increase the model input limits or grant authority.
If either bound would omit an authorized candidate, the catalog is incomplete and
selection is held; the router never labels a truncated candidate set complete. Its
JSON response must select one of those IDs with high confidence and provide
verbatim evidence; ambiguity, incomplete data and unknown IDs are held. Catalog, policy and source are
rechecked before delivery.

Delivery uses the public `chat.inject` RPC with the observed physical
`sessionId`. A Gateway implementing this guard must validate it atomically
with the append. Older Gateways that reject the parameter are not compatible;
do not retry without it. Context injection is not an agent turn. Existing work
intake is separate and remains governed by its own policy. The receipt journal
is written before the RPC and prevents automatic replay after an unknown result.

The worker invokes a pinned compiled isolated utility adapter and the existing
utility-model configuration. The R3 adapter currently admits only the Codex
isolated harness; other runtimes are held rather than silently changing models.
The Gateway transport uses the ordinary authenticated CLI bootstrap; its request
is passed on stdin, not in the operating-system process arguments. An in-memory
argument array is used by the standard CLI parser; no role or scope is overridden. Pinned paths and hashes, including the
helper hash, are operator-owned configuration and must be regenerated and
reviewed for each new immutable Gateway build. No credentials belong in those
files or command arguments. The scope and host configuration files must be
owned by the service user and have mode 0600.

The service and timer under `examples/semantic-systemd` are inactive templates,
not installation instructions. Enable only after independent review, strict
checks, verification of the current Gateway guard, a bounded synthetic utility
check, and a controlled end-to-end test. Do not copy or reset live mail data or
cursor. `agent_mail_semantic_worker.py --state-dir PATH --status` reads bounded,
secret-free counts of queued, processing, complete, held and invalid entries,
plus fixed held-reason codes and an observation of the worker lock. A processing
entry without an active lock is reported as interrupted; lock observations can
race with a worker starting or stopping. Status never calls a model or Gateway. Counts are
limited to 10,000 entries and report truncation.


Classifier evidence is constrained to deterministic exact excerpts from the complete original body, each at most160UTF-8bytes without splitting code points. Both adapters offer those literal strings in the prompt and dynamic output-schema enum; the decoded body/catalog limits stay12KiB/20KiB. This adds bounded model-input metadata, not authority. The original raw response remains unchanged for the strict Python JSON, duplicate-key and exact-grounding validator. Paraphrases and case-changed quotations remain rejected; no output repair or case folding is performed.


Lexical ranking ignores a bounded set of Russian/English grammatical connectors (for example, через/through). The original body and grounding tokenizer stay unchanged. Content-word ties remain held. A grammatical-only match filtered to zero cannot newly override a positive content competitor; the existing true-zero semantic control remains. Selector/cache versions, message admission gates and delivery journals are unchanged. Existing cached/held/unknown items are not reset or replayed by this update.


Evidence chunks prefer token boundaries so a bounded grounding word crossing a160-byte cut remains available whole. Original order and complete-body coverage are preserved; byte/code-point limits remain unchanged. A single token longer than160UTF-8bytes cannot fit under the existing evidence contract and is not made admissible by this change.
