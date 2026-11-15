"""Owner-scoped metadata catalog from authenticated Gateway sessions.list.

No mailbox content, transcript reads, model call or delivery occurs here. A
mailbox's scope is operator configuration; message headers never select it.
"""
from pathlib import Path
import hashlib
import json
import os
import stat
import re

from agent_mail_routing import RoutingScope, SessionFact, catalog, MAX_SESSIONS


def load_scope(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise ValueError('mail_scope_not_private') from None
    with os.fdopen(fd, 'rb') as stream:
        s = os.fstat(stream.fileno())
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                or stat.S_IMODE(s.st_mode) & 0o077 or s.st_size > 65536):
            raise ValueError('mail_scope_not_private')
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
        if (len(raw) > 65536 or (s.st_size, s.st_mtime_ns, s.st_mode)
                != (after.st_size, after.st_mtime_ns, after.st_mode)):
            raise ValueError('mail_scope_changed')
    config = json.loads(raw)
    required = ('owner_profile', 'account', 'owner_telegram_id')
    if (not isinstance(config, dict) or config.get('version') != 1
            or any(not isinstance(config.get(k), str) or not config[k].strip() for k in required)
            or not isinstance(config.get('allowed_agents'), list)
            or not config['allowed_agents'] or len(config['allowed_agents']) > 16
            or any(not isinstance(a, str) or not a.strip() for a in config['allowed_agents'])):
        raise ValueError('mail_scope_invalid')
    groups = config.get('report_group_grants', [])
    if (not isinstance(groups, list) or len(groups) > 256
            or any(not isinstance(g, dict) or set(g) != {'agent', 'session_key'}
                   or not all(isinstance(v, str) and v for v in g.values()) for g in groups)):
        raise ValueError('mail_group_grants_invalid')
    aliases = config.get('aliases', {})
    if (not isinstance(aliases, dict) or len(aliases) > MAX_SESSIONS
            or any(not isinstance(k, str) or not isinstance(v, list) or len(v) > 12
                   or any(not isinstance(x, str) or not x.strip() or len(x.encode()) > 256 for x in v)
                   for k, v in aliases.items())):
        raise ValueError('mail_aliases_invalid')
    paused = config.get('paused_sessions', [])
    if (not isinstance(paused, list) or len(paused) > MAX_SESSIONS
            or any(not isinstance(k, str) or not k for k in paused)):
        raise ValueError('mail_paused_sessions_invalid')
    revision = hashlib.sha256(raw).hexdigest()
    scope = RoutingScope(config['owner_profile'], config['account'], tuple(config['allowed_agents']),
                         tuple((g['agent'], g['session_key']) for g in groups), revision)
    return config, scope


def _same_human(identity, config):
    if not isinstance(identity, dict):
        return False
    if identity.get('type') == 'profile':
        return identity.get('id') == config['owner_profile']
    if identity.get('type') == 'observation':
        return (identity.get('pluginId') == 'telegram'
                and identity.get('accountId') == config['account']
                and identity.get('id') == config['owner_telegram_id'])
    return identity.get('type') == 'agent'


def _participants(row):
    # Full Gateway projection omits all three fields when only the owner exists.
    keys = ('participants', 'expandedParticipants', 'participantCount')
    if all(k not in row for k in keys):
        return []
    count = row.get('participantCount')
    members = row.get('expandedParticipants', row.get('participants'))
    if (type(count) is not int or count < 0 or not isinstance(members, list)
            or len(members) != count):
        return None
    summary = row.get('participants', [])
    if not isinstance(summary, list) or any(p not in members for p in summary):
        return None
    return members


def _direct_peer_is_owner(origin, config):
    if origin.get('nativeDirectUserId') is not None:
        return str(origin['nativeDirectUserId']) == config['owner_telegram_id']
    # Historical direct rows have canonical origin addresses, without the new
    # nativeDirectUserId field. The separately verified profile remains required.
    if origin.get('chatType') != 'direct':
        return False
    addresses = [origin[k] for k in ('from', 'to') if origin.get(k) is not None]
    if not addresses:
        return False
    for address in addresses:
        match = re.fullmatch(r'telegram:([0-9]+)(?::topic:[0-9]+)?', address) if isinstance(address, str) else None
        if not match or match[1] != config['owner_telegram_id']:
            return False
    return True


def session_fact(row, config, scope):
    """Return a fact only for a currently usable, expressly scoped destination."""
    if not isinstance(row, dict):
        raise ValueError('gateway_session_invalid')
    owner = row.get('owner') or {}
    actor = owner.get('actor') or {}
    identity = actor.get('identity') or {}
    key = row.get('key')
    agent = row.get('agentId')
    sid = row.get('sessionId')
    origin = row.get('origin') or {}
    if (identity.get('type') != 'profile' or identity.get('id') != scope.owner
            or row.get('sharingRole') != 'owner' or 'sendDisabledReason' not in row
            or row['sendDisabledReason'] is not None
            or agent not in scope.allowed_agents or not isinstance(key, str)
            or not isinstance(sid, str) or not sid or row.get('archived') is not False
            or row.get('isBackground') is not False or row.get('isMain') is not False
            or row.get('isDock') is not False or row.get('spawnedBy')
            or any(x in key for x in (':subagent:', ':cron:', ':dashboard:', ':incognito:'))
            or row.get('status') == 'paused' or row.get('classification') in ('dashboard', 'cron')):
        return None
    if key in config.get('paused_sessions', []):
        return None
    goal = row.get('goal')
    if goal is not None and (not isinstance(goal, dict) or goal.get('status') == 'paused'):
        return None
    origin_account = origin.get('accountId')
    last_account = row.get('lastAccountId')
    if (origin_account is not None and last_account is not None
            and origin_account != last_account):
        return None
    account = origin_account if origin_account is not None else last_account
    if account is not None and (not isinstance(account, str) or not account):
        return None
    provider, channel = origin.get('provider'), row.get('channel')
    normalized = lambda value: 'webui' if value in ('webchat', 'control-ui') else value
    if provider is not None and channel is not None and normalized(provider) != normalized(channel):
        return None
    kind = row.get('kind')
    if kind == 'direct':
        # Native Telegram direct chats must belong to this exact account/person.
        if origin.get('provider') == 'telegram' or row.get('channel') == 'telegram':
            if account != scope.account or not _direct_peer_is_owner(origin, config):
                return None
        elif origin.get('provider') in ('webchat', 'control-ui') or row.get('channel') in ('webchat', 'control-ui'):
            # Authenticated WebUI private sessions have the configured profile,
            # not a Telegram bot account. Explicit foreign accounts still deny.
            if account is not None and account != scope.account:
                return None
            account = scope.account
        else:
            return None
        participants = _participants(row)
        if not isinstance(participants, list) or any(
                not isinstance(p, dict) or not _same_human(p.get('identity'), config) for p in participants):
            return None
        fact_kind = 'private'
    elif kind == 'group':
        if account != scope.account or (agent, key) not in scope.group_grants:
            return None
        fact_kind = 'group'
    else:
        return None
    title = row.get('label') or row.get('displayName')
    if not isinstance(title, str) or not title.strip() or len(title.encode()) > 512:
        return None
    purpose = title  # metadata only; never fetch history to invent a purpose
    aliases = tuple(config.get('aliases', {}).get(key, []))
    return SessionFact(key, sid, scope.owner, account, agent, fact_kind, title, purpose,
                       aliases, True, True, True, False, False, False, False, False, False, False)


def collect(config, scope, rpc):
    """rpc must be the host's normal authenticated Gateway transport.

    A bounded single response avoids mutable offset pagination. Re-collect and
    revalidate the selected fact immediately before incarnation-fenced delivery.
    """
    page = rpc('sessions.list', {'limit': MAX_SESSIONS, 'offset': 0,
                    'source': 'sessions-search', 'profileRelation': {
                        'profileId': scope.owner, 'relationship': 'owned'},
                    'excludeSubagents': True, 'excludeCron': True, 'excludeSystem': True,
                    'excludeDock': True, 'configuredAgentsOnly': True,
                    'includeDerivedTitles': False, 'includeLastMessage': False})
    if not isinstance(page, dict) or not isinstance(page.get('sessions'), list):
        raise ValueError('catalog_response_invalid')
    rows = page['sessions']
    if (len(rows) > MAX_SESSIONS or page.get('hasMore') is not False
            or page.get('nextOffset') is not None or page.get('offset', 0) != 0
            or type(page.get('count')) is not int or page['count'] != len(rows)
            or type(page.get('totalCount')) is not int or page['totalCount'] != len(rows)):
        raise ValueError('catalog_incomplete')
    keys = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('key'), str) or row['key'] in keys:
            raise ValueError('catalog_duplicate_or_invalid_session')
        keys.add(row['key'])
    facts = tuple(f for row in rows if (f := session_fact(row, config, scope)) is not None)
    revision = hashlib.sha256(json.dumps([(f.session_key, f.incarnation, f.title, f.aliases)
                                         for f in facts], sort_keys=True).encode()).hexdigest()
    return catalog(scope, facts, revision=revision, pages_complete=True)
