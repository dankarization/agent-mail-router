"""Read-only admission observations for newly ingested mail.

Semantic selection stays closed until Gateway provides independently verifiable
owner-scoped access and incarnation-fenced delivery. This module never invokes
models, grants access, reserves a decision or creates an effect.
"""
from collections import Counter
from agent_mail_inbound import preflight
from agent_mail_gateway_stdio import GatewayPinnedStateChanged

MAX_MESSAGES = 100


def observe_new_ingest(store, messages):
    """Classify only keys in this poll; never revisit historical mail."""
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        return {"status": "held", "reason": "snapshot_invalid_or_too_large"}
    keys = []
    for item in messages:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            return {"status": "held", "reason": "snapshot_invalid"}
        keys.append(item["key"])
    if len(set(keys)) != len(keys):
        return {"status": "held", "reason": "snapshot_duplicate_keys"}
    reasons = Counter()
    for key in keys:
        result = preflight(store, key)
        reasons[result.reason] += 1
    return {"status": "held", "reason": "semantic_delivery_unavailable", "examined": len(keys),
            "reasons": dict(sorted(reasons.items()))}


def select_advisory(store, mail_key, verified_catalog, classifier):
    """Select once for a new persisted fallback, then hold; never enqueue delivery.

    The caller must provide an independently verified catalog. A Catalog is a
    data structure, not proof that such verification occurred. This seam has no
    production caller until an authenticated catalog adapter is available.
    """
    import json
    from agent_mail_routing import Catalog, SelectionInput, shortlist
    from agent_mail_routing_ledger import reserve, finalize

    if preflight(store, mail_key).reason != "atomic_incarnation_fence_unavailable":
        return {"status": "held", "reason": "mail_not_admissible"}
    if not isinstance(verified_catalog, Catalog) or verified_catalog.complete is not True or not callable(classifier):
        return {"status": "held", "reason": "catalog_unverified_or_incomplete"}
    try:
        row = store.db.execute("SELECT reason,payload FROM mail WHERE key=?", (mail_key,)).fetchone()
        msg = json.loads(row["payload"])
        if (msg.get("content_request") or msg.get("body_truncated") is not False
                or not isinstance(msg.get("subject"), str) or not isinstance(msg.get("body"), str)):
            return {"status": "held", "reason": "mail_body_incomplete"}
        source = SelectionInput(mail_key, row["reason"], row["reason"] == "general_external_report",
                                "unbound", "new", msg["subject"] + "\n" + msg["body"], True)
        options = shortlist(verified_catalog, source)
        claim = reserve(store.db, verified_catalog, source, options)
        if claim.status == "held":
            return {"status": "held", "reason": "classifier_reservation_held"}
        if claim.status == "completed":
            return {"status": "held", "reason": "delivery_not_enabled", "decision": claim.decision}
        # No DB transaction is kept open while the bridge runs. Any crash or
        # ambiguous bridge result leaves its reservation held, not replayable.
        result = classifier(source, options)
        current = store.db.execute("SELECT reason,payload FROM mail WHERE key=?", (mail_key,)).fetchone()
        if (getattr(result, "status", None) != "ok" or not isinstance(getattr(result, "decision_raw", None), str)
                or preflight(store, mail_key).reason != "atomic_incarnation_fence_unavailable"
                or current is None or (current["reason"], current["payload"]) != (row["reason"], row["payload"])):
            return {"status": "held", "reason": "classifier_or_source_changed"}
        decision = finalize(store.db, verified_catalog, source, options, claim.token, result.decision_raw)
        return {"status": "held", "reason": "delivery_not_enabled", "decision": decision}
    except Exception:
        return {"status": "held", "reason": "selection_unavailable"}


def process_semantic_context(store, mail_key, scope_path, rpc, classifier, *, queue_claim=None, content_prepare=None):
    """Process one newly persisted fallback outside the time-bounded mail tick.

    This explicit entry point does not schedule itself or grant work authority.
    It uses the configured catalog and a one-shot classifier reservation, then
    delegates passive context delivery to the incarnation-fenced Gateway seam.
    Unknown classifier and delivery outcomes remain held. The caller must run
    before ordinary batching, or supply a separately reviewed durable queue.
    """
    import json
    from agent_mail_gateway_catalog import load_scope, collect
    from agent_mail_gateway_delivery import deliver
    from agent_mail_routing import SelectionInput, shortlist
    from agent_mail_routing_ledger import reserve, finalize

    def snapshot():
        if queue_claim is None:
            if preflight(store, mail_key).reason != 'atomic_incarnation_fence_unavailable':
                return None
        elif not _queued_source_current(store, mail_key, queue_claim):
            return None
        row = store.db.execute('SELECT route,reason,payload FROM mail WHERE key=?', (mail_key,)).fetchone()
        if row is None:
            return None
        message = json.loads(row['payload'])
        if not isinstance(message, dict) or not isinstance(message.get('subject'), str):
            return None
        if message.get('content_request'):
            body = _full_content_body(store, message)
            if body is None:
                return None
        elif message.get('body_truncated') is False and isinstance(message.get('body'), str):
            body = message['body']
        else:
            return None
        source = SelectionInput(mail_key, row['reason'], row['reason'] == 'general_external_report',
                                'unbound', 'new', message['subject'] + '\n' + body, True)
        return ((row['route'], row['reason'], row['payload']), source)

    try:
        if queue_claim is not None:
            entry = store.db.execute('SELECT payload FROM mail WHERE key=?', (mail_key,)).fetchone()
            message = json.loads(entry[0]) if entry else None
            if isinstance(message, dict) and message.get('content_request'):
                if not _queued_source_current(store, mail_key, queue_claim):
                    return {'status': 'held', 'reason': 'mail_not_admissible'}
                if not callable(content_prepare) or content_prepare(mail_key) is not True:
                    return {'status': 'held', 'reason': 'content_unavailable'}
        initial = snapshot()
        if initial is None or not callable(rpc) or not callable(classifier):
            return {'status': 'held', 'reason': 'mail_not_admissible'}
        original, source = initial
        cfg, scope = load_scope(scope_path)
        selected_catalog = collect(cfg, scope, rpc)
        options = shortlist(selected_catalog, source)
        if not options.complete or not options.entries:
            return {'status': 'held', 'reason': 'catalog_incomplete_or_no_candidates'}
        current = snapshot()
        if current is None or current != (original, source):
            return {'status': 'held', 'reason': 'source_changed'}
        claim = reserve(store.db, selected_catalog, source, options)
        if claim.status == 'held':
            return {'status': 'held', 'reason': 'classifier_reservation_held'}
        if claim.status == 'completed':
            decision = claim.decision
        else:
            result = classifier(source, options)
            current = snapshot()
            if (current is None or current != (original, source) or getattr(result, 'status', None) != 'ok'
                    or not isinstance(getattr(result, 'decision_raw', None), str)):
                return {'status': 'held', 'reason': 'classifier_or_source_changed'}
            decision = finalize(store.db, selected_catalog, source, options, claim.token, result.decision_raw)
        if decision is None or decision.status != 'selected':
            return {'status': 'held', 'reason': 'classifier_did_not_select'}
        def source_is_current():
            current = snapshot()
            return current is not None and current == (original, source)
        return deliver(store.db, scope_path, selected_catalog, source, options, decision, rpc, source_is_current)
    except GatewayPinnedStateChanged:
        return {'status': 'held', 'reason': 'gateway_pinned_state_changed'}
    except Exception:
        return {'status': 'held', 'reason': 'semantic_processing_unavailable'}

_QUEUE_PREFIX = 'semantic_queue_v1:'


def _queue_key(mail_key):
    import hashlib
    return _QUEUE_PREFIX + hashlib.sha256(mail_key.encode('utf-8')).hexdigest()


def _queue_fingerprint(row):
    import hashlib
    import json
    value = json.dumps([row['route'], row['reason'], row['payload']], ensure_ascii=True,
                       separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def enqueue_new_ingest(store, messages, *, within_transaction=False):
    """Durably capture only eligible keys from this tick, before batch creation.

    No model calls or network. A completed/in-flight/unknown queue item is never
    replaced. Caller enables this explicitly and runs it after ingest/reroute.
    """
    import json
    if (not isinstance(messages, list) or len(messages) > MAX_MESSAGES
            or any(not isinstance(m, dict) or not isinstance(m.get('key'), str)
                   or not m['key'] for m in messages)):
        return {'status': 'held', 'reason': 'snapshot_invalid'}
    keys = [m['key'] for m in messages]
    if len(set(keys)) != len(keys):
        return {'status': 'held', 'reason': 'snapshot_duplicate_keys'}
    if within_transaction != store.db.in_transaction:
        return {'status': 'held', 'reason': 'transaction_owned'}
    added = 0
    if not within_transaction:
        store.db.execute('BEGIN IMMEDIATE')
    try:
        for key in keys:
            if preflight(store, key).reason != 'atomic_incarnation_fence_unavailable':
                continue
            row = store.db.execute('SELECT route,reason,payload FROM mail WHERE key=?', (key,)).fetchone()
            message = json.loads(row['payload'])
            if (not isinstance(message, dict) or not isinstance(message.get('subject'), str)
                    or not isinstance(message.get('body'), str)):
                continue
            if message.get('content_request'):
                from agent_mail_content import source_for
                if message['content_request'] != source_for(message):
                    continue
            elif message.get('body_truncated') is not False:
                continue
            record = {'version': 1, 'status': 'queued', 'mailKey': key, 'fingerprint': _queue_fingerprint(row)}
            added += store.db.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                (_queue_key(key), json.dumps(record, sort_keys=True, separators=(',', ':')))).rowcount
        if not within_transaction:
            store.db.commit()
    except Exception:
        if within_transaction:
            raise
        store.db.rollback()
        return {'status': 'held', 'reason': 'queue_write_failed'}
    return {'status': 'ok', 'queued': added, 'examined': len(keys)}


def _queued_source_current(store, mail_key, claim):
    import json
    try:
        entry = store.db.execute('SELECT value FROM meta WHERE key=?', (_queue_key(mail_key),)).fetchone()
        record = json.loads(entry[0]) if entry else None
        if (not isinstance(record, dict) or record != claim or record.get('version') != 1
                or record.get('status') != 'processing' or record.get('mailKey') != mail_key):
            return False
        row = store.db.execute('SELECT route,reason,payload FROM mail WHERE key=?', (mail_key,)).fetchone()
        if row is None or _queue_fingerprint(row) != record.get('fingerprint'):
            return False
        msg = json.loads(row['payload'])
        if callable(getattr(store, 'reload_routes', None)):
            store.routes = store.reload_routes()
        route = store.routes.get(row['route'])
        return (isinstance(msg, dict) and msg.get('key') == mail_key
                and row['reason'] in ('general_fallback', 'general_external_report')
                and isinstance(route, dict) and route.get('fallback') is True
                and store.choose(msg) == (row['route'], row['reason']))
    except Exception:
        return False


def run_one_queued(store, scope_path, rpc, classifier, *, content_prepare=None):
    """Claim at most one pre-batch entry. Crash or uncertain outcome is held."""
    import json
    import secrets
    if store.db.in_transaction:
        return {'status': 'held', 'reason': 'transaction_owned'}
    store.db.execute('BEGIN IMMEDIATE')
    try:
        rows = store.db.execute('SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key',
                                (_QUEUE_PREFIX + '%',)).fetchall()
        claim = None
        for row in rows:
            try:
                record = json.loads(row[1])
            except (TypeError, ValueError):
                continue
            if (isinstance(record, dict) and set(record) == {'version', 'status', 'mailKey', 'fingerprint'}
                    and record['version'] == 1 and record['status'] == 'queued'
                    and isinstance(record['mailKey'], str) and isinstance(record['fingerprint'], str)
                    and row[0] == _queue_key(record['mailKey'])):
                claim = dict(record, status='processing', token=secrets.token_hex(32))
                encoded = json.dumps(claim, sort_keys=True, separators=(',', ':'))
                store.db.execute('UPDATE meta SET value=? WHERE key=? AND value=?', (encoded, row[0], row[1]))
                break
        store.db.commit()
    except Exception:
        store.db.rollback()
        return {'status': 'held', 'reason': 'queue_claim_failed'}
    if claim is None:
        return {'status': 'idle'}
    result = process_semantic_context(store, claim['mailKey'], scope_path, rpc, classifier, queue_claim=claim, content_prepare=content_prepare)
    # Compare-and-swap the claim; never turn an unknown/interrupted claim back into queued.
    try:
        final = dict(claim, status='complete' if result.get('status') in ('delivered', 'already_delivered') else 'held',
                     outcome=result.get('reason', result.get('status', 'unknown')))
        encoded_claim = json.dumps(claim, sort_keys=True, separators=(',', ':'))
        encoded_final = json.dumps(final, sort_keys=True, separators=(',', ':'))
        with store.db:
            count = store.db.execute('UPDATE meta SET value=? WHERE key=? AND value=?',
                      (encoded_final, _queue_key(claim['mailKey']), encoded_claim)).rowcount
        if count != 1:
            return {'status': 'held', 'reason': 'queue_claim_changed'}
    except Exception:
        return {'status': 'held', 'reason': 'queue_finalization_unknown'}
    return result


def _full_content_body(store, message):
    """Read a complete, integrity-checked MIME body as untrusted data."""
    from pathlib import Path
    from agent_mail_content import CONTENT_ROOT, identity, read_manifest, read_text, sha
    from agent_mail_routing import MAX_TEXT_BYTES
    try:
        root = Path(getattr(store, 'content_root', CONTENT_ROOT)).resolve(strict=True)
        manifest = read_manifest(root, message)
        if manifest.get('state') not in ('available', 'available_with_warnings'):
            return None
        original = manifest.get('original') or {}
        original_path = Path(original.get('path', '')).resolve(strict=True)
        job = (root / identity(message)).resolve(strict=True)
        if not original_path.is_relative_to(job) or sha(original_path) != original.get('sha256'):
            return None
        parts = [p for p in manifest.get('parts', []) if p.get('role') == 'body']
        plain = [p for p in parts if p.get('mime') == 'text/plain']
        chosen = plain if plain else [p for p in parts if p.get('mime') == 'text/html']
        if len(chosen) != 1 or chosen[0].get('state') != 'extracted':
            return None
        part = read_text(root, message, chosen[0]['id'])
        body = part.get('untrusted_text')
        if (not isinstance(body, str) or part.get('has_more') is not False
                or len(body.encode('utf-8')) > MAX_TEXT_BYTES
                or part.get('source_sha256') != original['sha256']):
            return None
        return body
    except Exception:
        return None


def queue_status(db):
    """Secret-free bounded queue health; processing means interrupted or active."""
    import json
    from collections import Counter
    allowed = {'mail_not_admissible', 'content_unavailable', 'catalog_incomplete_or_no_candidates',
               'source_changed', 'classifier_reservation_held', 'classifier_or_source_changed',
               'classifier_did_not_select', 'scope_or_source_changed', 'destination_catalog_changed',
               'destination_changed', 'source_changed_during_admission', 'previous_delivery_outcome',
               'delivery_outcome_unknown', 'delivery_receipt_unknown', 'semantic_processing_unavailable',
               'queue_claim_changed', 'queue_finalization_unknown'}
    counts=Counter(); reasons=Counter()
    rows=db.execute('SELECT value FROM meta WHERE key LIKE ? ORDER BY key LIMIT 10001',
                    (_QUEUE_PREFIX+'%',)).fetchall()
    for row in rows[:10000]:
        try:
            record=json.loads(row[0]); state=record.get('status')
            if record.get('version') != 1 or state not in ('queued','processing','complete','held'):
                raise ValueError('invalid')
            counts[state]+=1
            if state=='held':
                reason=record.get('outcome'); reasons[reason if reason in allowed else 'other']+=1
        except (ValueError,TypeError,AttributeError):
            counts['invalid']+=1
    return {'status':'ok','counts':{k:counts[k] for k in ('queued','processing','complete','held','invalid')},
            'heldReasons':dict(sorted(reasons.items())),'truncated':len(rows)>10000}
