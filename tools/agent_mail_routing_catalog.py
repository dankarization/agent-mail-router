"""Owner-filtered catalog RPC contract. Transport and ACL proof are injected.

The existing Gateway client may be injected; authority proof remains separate.
"""
from dataclasses import dataclass
from typing import Callable, Protocol

from agent_mail_routing import Catalog, RoutingScope, SessionFact, catalog


class CatalogPageProvider(Protocol):
    def fetch(self, params: dict) -> dict: ...


class GatewayCatalogProvider:
    """Use the existing host Gateway.call transport for read-only catalog pages.

    Listing rows is not authorization: collect still requires a separate verifier.
    The owner of this object controls credentials, deadline and client lifecycle.
    """
    def __init__(self, gateway):
        if not callable(getattr(gateway, 'call', None)):
            raise ValueError('catalog_gateway_invalid')
        self.gateway = gateway

    def fetch(self, params: dict) -> dict:
        return self.gateway.call('sessions.list', params)


@dataclass(frozen=True)
class AccessProof:
    """Verified independently of sessions.list owner/membership metadata."""
    source: str  # scoped_acl or exact_operator_grant
    owner: str
    account: str
    agent: str
    session_key: str
    incarnation: str
    visible: bool
    can_read: bool
    can_deliver: bool


@dataclass(frozen=True)
class ProjectedSession:
    fact: SessionFact
    proof: AccessProof


# This callback is host-owned and may only use proven ACL/route evidence; the
# raw RPC row's owner, participants and membership alone are never a grant.
SessionVerifier = Callable[[dict, RoutingScope, str], ProjectedSession | None]


def page_params(owner: str, agent: str, offset: int, limit: int) -> dict:
    return {'agentId': agent, 'profileRelation': {'profileId': owner, 'relationship': 'owned'},
            'archived': False, 'excludeSubagents': True, 'excludeCron': True,
            'excludeSystem': True, 'excludeDock': True, 'configuredAgentsOnly': True,
            'rowMode': 'sidebar', 'includeDerivedTitles': False, 'includeLastMessage': False,
            'includeActivitySummary': False, 'limit': limit, 'offset': offset}


def collect(provider: CatalogPageProvider, scope: RoutingScope, verifier: SessionVerifier,
            *, revision: str, page_size: int = 100, max_pages: int = 10) -> Catalog:
    """Read bounded pages; incomplete or invalid traversal cannot reach a model."""
    if (not isinstance(scope, RoutingScope) or type(page_size) is not int or not 1 <= page_size <= 100
            or type(max_pages) is not int or not 1 <= max_pages <= 10 or not callable(verifier)):
        raise ValueError('catalog_request_invalid')
    # Validate operator-owned scope before issuing the first host callback.
    catalog(scope, (), revision=revision, pages_complete=False)
    facts = []
    complete = True
    page_count = 0
    for agent in dict.fromkeys(scope.allowed_agents):
        offset = 0
        finished = False
        expected_total = None
        for _ in range(max_pages):
            if page_count >= max_pages:
                break
            page_count += 1
            try:
                page = provider.fetch(page_params(scope.owner, agent, offset, page_size))
                if not isinstance(page, dict) or not isinstance(page.get('sessions'), list):
                    break
                rows = page['sessions']; more = page.get('hasMore'); total = page.get('totalCount')
                if (len(rows) > page_size or type(more) is not bool
                        or (total is not None and (type(total) is not int or total < offset + len(rows)))
                        or (expected_total is not None and total != expected_total)):
                    break
                if total is not None:
                    expected_total = total
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError('catalog_row_invalid')
                    owner = row.get('owner'); actor = owner.get('actor') if isinstance(owner, dict) else None
                    key, incarnation = row.get('key'), row.get('sessionId')
                    # Do not project or expose descriptions of foreign/ownerless rows.
                    if (not isinstance(actor, dict) or actor.get('type') != 'human'
                            or actor.get('id') != scope.owner or not isinstance(key, str)
                            or not key.startswith('agent:' + agent + ':')
                            or not isinstance(incarnation, str) or not incarnation):
                        continue
                    projected = verifier(row, scope, agent)
                    if projected is None:
                        continue
                    if not isinstance(projected, ProjectedSession) or not isinstance(projected.proof, AccessProof):
                        raise ValueError('catalog_proof_invalid')
                    fact, proof = projected.fact, projected.proof
                    if (not isinstance(fact, SessionFact) or proof.source not in ('scoped_acl', 'exact_operator_grant')
                            or (proof.owner, proof.account, proof.agent, proof.session_key, proof.incarnation)
                            != (scope.owner, scope.account, agent, key, incarnation)
                            or (fact.owner, fact.account, fact.agent, fact.session_key, fact.incarnation)
                            != (proof.owner, proof.account, proof.agent, proof.session_key, proof.incarnation)
                            or proof.visible is not True or proof.can_read is not True or proof.can_deliver is not True):
                        continue
                    facts.append(fact)
                next_offset = page.get('nextOffset')
                if more:
                    if not rows or type(next_offset) is not int or next_offset != offset + len(rows):
                        break
                    offset = next_offset
                else:
                    if total is not None and total != offset + len(rows):
                        break
                    finished = True
                    break
            except Exception:
                break
        if not finished:
            complete = False
            break
    try:
        return catalog(scope, tuple(facts), revision=revision, pages_complete=complete)
    except ValueError:
        # Malformed/duplicate allowed rows make the entire snapshot unusable.
        return catalog(scope, (), revision=revision, pages_complete=False)
