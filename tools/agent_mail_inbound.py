"""Fail-closed inbound integration boundary pending a fenced Gateway delivery.

This module does not call a classifier or enqueue effects. Existing router
behavior remains unchanged until an independently reviewed integration can
atomically enforce the selected incarnation and current delivery authority.
"""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class Admission:
    status: str
    reason: str


def preflight(store, mail_key: str) -> Admission:
    """Inspect a persisted mail row without granting authority or changing it.

    A current, unsummarized fallback row is still held: current chat.inject has
    no expected-incarnation wire field. No observation here is a delivery proof.
    """
    if not isinstance(mail_key, str) or not mail_key:
        return Admission('held', 'mail_identity_invalid')
    try:
        row = store.db.execute('SELECT * FROM mail WHERE key=?', (mail_key,)).fetchone()
        if row is None:
            return Admission('held', 'mail_missing')
        if row['summary_id'] is not None:
            return Admission('held', 'already_summarized_or_effect_unknown')
        for batch in store.db.execute('SELECT keys_json FROM batches'):
            keys = json.loads(batch['keys_json'])
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                return Admission('held', 'batch_state_invalid')
            if mail_key in keys:
                return Admission('held', 'already_batched')
        message = json.loads(row['payload'])
        if not isinstance(message, dict) or message.get('key') != mail_key:
            return Admission('held', 'mail_payload_invalid')
        if row['reason'] not in ('general_fallback', 'general_external_report'):
            return Admission('held', 'exact_or_triage')
        route = store.routes.get(row['route'])
        if not isinstance(route, dict) or route.get('fallback') is not True:
            return Admission('held', 'fallback_policy_changed')
        # Re-evaluate persisted source and current exact routes before any future
        # classifier call. A mismatch includes newly registered exact bindings.
        selected, reason = store.choose(message)
        if (selected, reason) != (row['route'], row['reason']):
            return Admission('held', 'exact_or_policy_changed')
        return Admission('held', 'atomic_incarnation_fence_unavailable')
    except Exception:
        return Admission('held', 'mail_state_invalid')

@dataclass(frozen=True)
class FallbackObservation:
    """Caller-visible metadata only; not a profile, ACL or delivery grant."""
    owner_id: str
    session_id: str


def observe_fallback(gateway, routes) -> FallbackObservation | None:
    """Read the exact operator fallback through the ordinary sessions.list RPC.

    This uses neither mail fields nor identity headers. A visible owner/creator
    match is an observation only: RPC projection does not attest actor source,
    audience, delivery permission or an atomic fence. No result enables routing.
    """
    try:
        if not isinstance(routes, dict):
            return None
        fallbacks = [r for r in routes.values() if isinstance(r, dict) and r.get('fallback') is True]
        if len(fallbacks) != 1:
            return None
        key = fallbacks[0].get('parent')
        if not isinstance(key, str) or not key or len(key) > 1024:
            return None
        result = gateway.call('sessions.list', {'search': key, 'limit': 10, 'rowMode': 'sidebar',
                                                'includeDerivedTitles': False, 'includeLastMessage': False,
                                                'includeActivitySummary': False})
        if (not isinstance(result, dict) or not isinstance(result.get('sessions'), list)
                or result.get('hasMore') is not False or result.get('truncationReason')):
            return None
        rows = [row for row in result['sessions'] if isinstance(row, dict) and row.get('key') == key]
        if len(rows) != 1:
            return None
        row = rows[0]
        owner = row.get('owner'); actor = owner.get('actor') if isinstance(owner, dict) else None
        creator = row.get('createdActor'); incarnation = row.get('sessionId')
        if (not isinstance(actor, dict) or not isinstance(creator, dict)
                or actor.get('type') != 'human' or creator.get('type') != 'human'
                or not isinstance(actor.get('id'), str) or not actor['id']
                or actor['id'] != creator.get('id')
                or not isinstance(incarnation, str) or not incarnation
                or row.get('archived') or row.get('incognito')):
            return None
        return FallbackObservation(actor['id'], incarnation)
    except Exception:
        return None
