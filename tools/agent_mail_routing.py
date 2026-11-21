"""Pure, fail-closed routing contract. The caller supplies verified host facts.

No Gateway, model, persistence, delivery, work admission or mailbox access occurs
here. A semantic match is advisory context routing, never execution authority.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol

MAX_SESSIONS = 4096
MAX_CANDIDATES = 128
MAX_TEXT_BYTES = 12 * 1024
MAX_CATALOG_BYTES = 20 * 1024
MIN_SEMANTIC_EVIDENCE_CHARS = 16
MAX_OUTPUT_BYTES = 4096
_SELECTOR_VERSION = 'semantic-routing-v1'
_ID = re.compile(r'c_[a-f0-9]{32}\Z')
_REASON = {'matched', 'no_match', 'ambiguous', 'insufficient_evidence'}
_SEMANTIC_INTAKE = {'general_fallback', 'general_external_report'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _text(value, limit):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode('utf-8')) <= limit
    except UnicodeEncodeError:
        return False


def _tokens(value):
    return set(re.findall(r'[^\W_]{3,}', value.casefold(), flags=re.UNICODE))


@dataclass(frozen=True)
class RoutingScope:
    """Operator-owned scope; values must come from the trusted host, not mail."""
    owner: str
    account: str
    allowed_agents: tuple[str, ...]
    group_grants: tuple[tuple[str, str], ...]  # exact (agent, canonical session key)
    policy_revision: str


@dataclass(frozen=True)
class SessionFact:
    """An adapter's per-session assertions; missing or unverified facts fail closed."""
    session_key: str
    incarnation: str
    owner: str
    account: str
    agent: str
    kind: str  # private or group
    title: str
    purpose: str
    aliases: tuple[str, ...]
    visible: bool
    can_read: bool
    can_deliver: bool
    archived: bool
    deleted: bool
    incognito: bool
    child: bool
    cron: bool
    dashboard: bool
    paused: bool


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    session_key: str  # private host binding; never included in model_input
    incarnation: str
    agent: str
    title: str
    purpose: str
    aliases: tuple[str, ...]

    def public(self):
        return {'candidateId': self.candidate_id, 'agentId': self.agent,
                'title': self.title, 'purpose': self.purpose, 'aliases': list(self.aliases)}


@dataclass(frozen=True)
class Catalog:
    scope: RoutingScope
    revision: str
    candidates: tuple[Candidate, ...]
    complete: bool


def catalog(scope: RoutingScope, sessions: tuple[SessionFact, ...], *, revision: str,
            pages_complete: bool) -> Catalog:
    """Filter trusted host facts BEFORE exposing metadata to a classifier.

    This is not an ACL implementation: true flags must be verified by a host
    adapter. No group is admitted on visibility or membership alone.
    """
    if (not isinstance(scope, RoutingScope) or not _text(scope.owner, 256)
            or not _text(scope.account, 256) or not _text(scope.policy_revision, 256)
            or not _text(revision, 256) or not isinstance(scope.allowed_agents, tuple)
            or not scope.allowed_agents or len(scope.allowed_agents) > 16
            or any(not _text(x, 128) for x in scope.allowed_agents)
            or not isinstance(scope.group_grants, tuple) or len(scope.group_grants) > 256
            or any(not isinstance(x, tuple) or len(x) != 2 or not all(_text(y, 512) for y in x)
                   for x in scope.group_grants)
            or type(pages_complete) is not bool or not isinstance(sessions, tuple)
            or len(sessions) > MAX_SESSIONS):
        raise ValueError('routing_scope_invalid')
    out = []
    seen = set()
    for row in sessions:
        if not isinstance(row, SessionFact):
            raise ValueError('session_fact_invalid')
        # Identity and all deny flags are checked before touching descriptive text.
        if (row.owner != scope.owner or row.account != scope.account or row.agent not in scope.allowed_agents
                or row.kind not in ('private', 'group') or row.visible is not True
                or row.can_read is not True or row.can_deliver is not True
                or any(flag is not False for flag in (row.archived, row.deleted, row.incognito,
                                                       row.child, row.cron, row.dashboard, row.paused))
                or not _text(row.session_key, 512) or not _text(row.incarnation, 256)
                or (row.kind == 'group' and (row.agent, row.session_key) not in scope.group_grants)):
            continue
        if (not _text(row.title, 512) or not _text(row.purpose, 1024)
                or not isinstance(row.aliases, tuple) or len(row.aliases) > 12
                or any(not _text(alias, 256) for alias in row.aliases)):
            raise ValueError('candidate_metadata_invalid')
        identity = (row.agent, row.session_key)
        if identity in seen:
            raise ValueError('duplicate_session')
        seen.add(identity)
        cid = 'c_' + _digest([scope.owner, scope.account, scope.policy_revision,
                              row.agent, row.session_key, row.incarnation])[:32]
        out.append(Candidate(cid, row.session_key, row.incarnation, row.agent,
                             row.title, row.purpose, row.aliases))
    return Catalog(scope, revision, tuple(sorted(out, key=lambda x: x.candidate_id)), pages_complete)


@dataclass(frozen=True)
class SelectionInput:
    """Clean new message text; source identity and provenance remain independent."""
    mail_identity: str
    intake_reason: str
    source_report_only: bool
    exact_status: str  # unbound, matched, conflict, invalid, revoked or unknown
    delivery_state: str  # new, unknown or sent
    body: str
    body_complete: bool


def _source_digest(source: SelectionInput) -> str:
    return _digest([source.mail_identity, source.intake_reason, source.source_report_only,
                    source.exact_status, source.delivery_state, source.body, source.body_complete])


@dataclass(frozen=True)
class Shortlist:
    entries: tuple[Candidate, ...]
    complete: bool
    scores: tuple[tuple[str, int], ...]
    source_digest: str


def shortlist(cat: Catalog, source: SelectionInput) -> Shortlist:
    if (not isinstance(cat, Catalog) or not isinstance(source, SelectionInput)
            or not _text(source.mail_identity, 512) or not _text(source.intake_reason, 128)
            or type(source.source_report_only) is not bool or type(source.body_complete) is not bool
            or source.exact_status not in ('unbound', 'matched', 'conflict', 'invalid', 'revoked', 'unknown')
            or source.delivery_state not in ('new', 'unknown', 'sent')
            or not _text(source.body, MAX_TEXT_BYTES)):
        raise ValueError('selection_input_invalid')
    if (source.exact_status != 'unbound' or source.delivery_state != 'new'
            or source.intake_reason not in _SEMANTIC_INTAKE):
        return Shortlist((), False, (), _source_digest(source))
    words = _tokens(source.body)
    ranked = []
    for entry in cat.candidates:
        score = len(words & _tokens(' '.join((entry.title, entry.purpose, *entry.aliases))))
        # Keep every already-authorized candidate in the bounded catalog.
        # A zero lexical score is not evidence that a semantic candidate is irrelevant.
        ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], item[1].candidate_id))
    selected = []
    size = 2
    for score, entry in ranked:
        amount = len(_canonical(entry.public()).encode('utf-8')) + 1
        if len(selected) == MAX_CANDIDATES or size + amount > MAX_CATALOG_BYTES:
            break
        selected.append((score, entry))
        size += amount
    return Shortlist(tuple(e for _, e in selected),
                     cat.complete and source.body_complete and len(selected) == len(ranked),
                     tuple((e.candidate_id, score) for score, e in selected), _source_digest(source))


def model_input(source: SelectionInput, options: Shortlist):
    """Only bounded untrusted data and public closed-set descriptors."""
    if (options.source_digest != _source_digest(source) or not options.complete or not options.entries
            or source.exact_status != 'unbound'
            or source.delivery_state != 'new' or source.intake_reason not in _SEMANTIC_INTAKE):
        raise ValueError('selection_not_admitted')
    return {'body': source.body, 'bodyComplete': source.body_complete,
            'catalogComplete': options.complete, 'candidates': [x.public() for x in options.entries]}


@dataclass(frozen=True)
class Decision:
    status: str  # selected or held
    candidate_id: str | None
    reason: str
    intake_reason: str
    source_report_only: bool


def _held(reason, source):
    return Decision('held', None, reason, source.intake_reason, source.source_report_only)


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate_key')
        out[key] = value
    return out


def decide(source: SelectionInput, options: Shortlist, raw: str) -> Decision:
    """Validate strict JSON and return an advisory decision; failures are held."""
    if (source.exact_status != 'unbound' or source.delivery_state != 'new'
            or source.intake_reason not in _SEMANTIC_INTAKE):
        return _held('existing_binding_or_delivery', source)
    try:
        if options.source_digest != _source_digest(source):
            return _held('input_changed', source)
    except (TypeError, ValueError, UnicodeError):
        return _held('invalid_input', source)
    if not options.complete:
        return _held('incomplete_input', source)
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_OUTPUT_BYTES:
            return _held('invalid_output', source)
    except UnicodeEncodeError:
        return _held('invalid_output', source)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (ValueError, RecursionError):
        return _held('invalid_output', source)
    if not isinstance(value, dict) or set(value) != {'candidateId', 'confidence', 'alternatives', 'evidence', 'reasonCode'}:
        return _held('invalid_output', source)
    cid, confidence, alternatives, evidence, reason = (value[k] for k in
        ('candidateId', 'confidence', 'alternatives', 'evidence', 'reasonCode'))
    ids = {entry.candidate_id for entry in options.entries}
    if (not (cid is None or isinstance(cid, str) and _ID.fullmatch(cid) and cid in ids)
            or confidence not in ('high', 'medium', 'low')
            or not isinstance(alternatives, list) or len(alternatives) > 2
            or any(not isinstance(x, str) or x not in ids or x == cid for x in alternatives)
            or len(set(alternatives)) != len(alternatives)
            or not isinstance(evidence, list) or len(evidence) > 4
            or any(not _text(x, 160) or x not in source.body for x in evidence)
            or not isinstance(reason, str) or reason not in _REASON):
        return _held('invalid_output', source)
    if cid is None:
        return _held('abstained', source) if reason != 'matched' else _held('invalid_output', source)
    if reason != 'matched' or confidence != 'high' or alternatives or not evidence:
        return _held('ambiguous', source)
    scores = dict(options.scores)
    if cid not in scores:
        return _held('invalid_output', source)
    selected = scores[cid]
    if selected < 0:
        return _held('invalid_output', source)
    competing = [score for other, score in options.scores if other != cid]
    if selected == 0:
        semantic_override = True
    elif any(score >= selected for score in competing):
        return _held('ambiguous', source)
    elif any(selected - score == 1 for score in competing):
        # Only a one-token lexical lead over zero-score candidates can be
        # resolved semantically; positive close competitors remain ambiguous.
        if selected != 1 or any(score != 0 for score in competing):
            return _held('ambiguous', source)
        semantic_override = True
    else:
        semantic_override = False
    if semantic_override:
        if not any(len(span.strip()) >= MIN_SEMANTIC_EVIDENCE_CHARS for span in evidence):
            return _held('insufficient_evidence', source)
    elif not any(_tokens(span) & _tokens(' '.join((entry.title, entry.purpose, *entry.aliases)))
                 for entry in options.entries if entry.candidate_id == cid for span in evidence):
        return _held('insufficient_evidence', source)
    return Decision('selected', cid, 'matched', source.intake_reason, source.source_report_only)


def cache_key(cat: Catalog, source: SelectionInput, options: Shortlist) -> str:
    """Bind a host-owned cache record to identity, text, scope and exact snapshot."""
    if shortlist(cat, source) != options:
        raise ValueError('shortlist_changed')
    return _digest({'version': _SELECTOR_VERSION, 'owner': cat.scope.owner,
                    'account': cat.scope.account, 'policy_revision': cat.scope.policy_revision,
                    'allowed_agents': cat.scope.allowed_agents, 'group_grants': cat.scope.group_grants,
                    'catalog_revision': cat.revision, 'catalog_complete': cat.complete,
                    'mail_identity': source.mail_identity, 'body_digest': _digest(source.body),
                    'body_complete': source.body_complete, 'intake_reason': source.intake_reason,
                    'source_report_only': source.source_report_only, 'exact_status': source.exact_status,
                    'delivery_state': source.delivery_state, 'shortlist_complete': options.complete,
                    'catalog_snapshot': [entry.public() | {'incarnation': entry.incarnation}
                                         for entry in cat.candidates],
                    'candidates': [entry.candidate_id for entry in options.entries]})


class DecisionCache(Protocol):
    """Host-owned cache; persistence, leases and transactional admission are external."""
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, validated_output: str) -> None: ...
