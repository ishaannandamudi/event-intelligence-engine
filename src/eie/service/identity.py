"""Reuse exact foundation resolution over the explicitly published identity universe."""

from eie.entities.models import Issuer, Listing, Security
from eie.entities.protocols import ResolutionState
from eie.entities.registry import InMemoryEntityRegistry
from eie.entities.sec import SecIssuerRevision, SecListingRevision
from eie.events.scope import EventScope, ScopeKind
from eie.graph.nodes import GraphNode
from eie.service import owners
from eie.service.errors import ServiceError
from eie.service.models import IdentityBinding, Publication, SnapshotRequest
from eie.service.repository import ServiceRepository
from eie.service.schema import ComponentKind, OwnerKind, ServiceCode


def resolve(
    repository: ServiceRepository, request: SnapshotRequest
) -> tuple[IdentityBinding, tuple[EventScope, ...], tuple[Publication, ...]]:
    kind = request.scope.kind
    broad = (EventScope(ScopeKind.MARKET), EventScope(ScopeKind.MACRO_GLOBAL))
    if kind not in (ScopeKind.SYMBOL, ScopeKind.COMPANY, ScopeKind.SECURITY):
        return (
            IdentityBinding(),
            tuple(sorted({request.scope, *broad}, key=lambda s: (s.kind.value, s.identifiers))),
            (),
        )
    publications: dict[str, Publication] = {}

    def nodes(scope: EventScope) -> tuple[GraphNode, ...]:
        values = repository.collect(request, ComponentKind.IDENTITY, (scope,), identity_only=True)
        result = []
        for p in values:
            if p.reference.kind is not OwnerKind.NODE:
                continue
            node = owners.read(repository.store, OwnerKind.NODE, p.reference.lookup)
            assert isinstance(node, GraphNode)
            publications[node.node_id] = p
            result.append(node)
        return tuple(result)

    initial = nodes(request.scope)
    if kind is ScopeKind.SYMBOL:
        listings = tuple(
            n
            for n in initial
            if isinstance(n.foundation, SecListingRevision)
            and n.foundation.exchange == request.exchange
            and n.foundation.ticker == request.scope.identifiers[0]
            and n.foundation.valid_from_ns <= request.valid_ns
            and (n.foundation.valid_to_ns is None or request.valid_ns < n.foundation.valid_to_ns)
        )
        if not listings:
            raise ServiceError(ServiceCode.HISTORICAL_DATA_UNAVAILABLE)
        for node in listings:
            assert isinstance(node.foundation, SecListingRevision)
            nodes(EventScope(ScopeKind.COMPANY, (node.foundation.issuer_id,)))
            nodes(EventScope(ScopeKind.SECURITY, (node.foundation.security_id,)))
        registry = InMemoryEntityRegistry()
        materialized = tuple(
            owners.read(repository.store, OwnerKind.NODE, p.reference.lookup)
            for p in publications.values()
        )
        for identity_node in materialized:
            assert isinstance(identity_node, GraphNode)
            foundation = identity_node.foundation
            if isinstance(foundation, Issuer):
                registry.add_issuer(foundation)
            elif isinstance(foundation, SecIssuerRevision):
                registry.add_issuer(
                    Issuer(
                        foundation.issuer_id,
                        foundation.legal_name,
                        foundation.cik.digits,
                        foundation.aliases,
                    )
                )
        for identity_node in materialized:
            assert isinstance(identity_node, GraphNode)
            if isinstance(identity_node.foundation, Security):
                registry.add_security(identity_node.foundation)
        for node in listings:
            f = node.foundation
            assert isinstance(f, SecListingRevision)
            registry.add_listing(
                Listing(
                    f.security_id,
                    f.ticker,
                    f.exchange,
                    f.valid_from_ns,
                    f.valid_to_ns,
                    f.available_from_ns,
                )
            )
        resolved = registry.resolve(
            request.scope.identifiers[0], request.known_as_of_ns, request.valid_ns
        )
        if resolved.state is ResolutionState.AMBIGUOUS:
            raise ServiceError(ServiceCode.AMBIGUOUS_IDENTITY)
        if resolved.state is not ResolutionState.RESOLVED:
            raise ServiceError(ServiceCode.HISTORICAL_DATA_UNAVAILABLE)
        security_id = resolved.candidate_ids[0]
        match = next(
            n
            for n in listings
            if isinstance(n.foundation, SecListingRevision)
            and n.foundation.security_id == security_id
        )
        assert isinstance(match.foundation, SecListingRevision)
        issuer_id, listing_id = match.foundation.issuer_id, match.foundation.listing_id
    elif kind is ScopeKind.SECURITY:
        security_node = next(
            (
                n
                for n in initial
                if isinstance(n.foundation, Security) and n.node_id == request.scope.identifiers[0]
            ),
            None,
        )
        if security_node is None:
            raise ServiceError(ServiceCode.NOT_FOUND)
        assert isinstance(security_node.foundation, Security)
        security_id, issuer_id, listing_id = (
            security_node.node_id,
            security_node.foundation.issuer_id,
            None,
        )
        if not any(
            n.node_id == issuer_id for n in nodes(EventScope(ScopeKind.COMPANY, (issuer_id,)))
        ):
            raise ServiceError(ServiceCode.HISTORICAL_DATA_UNAVAILABLE)
    else:
        issuer_id, security_id, listing_id = request.scope.identifiers[0], None, None
        if not any(
            n.node_id == issuer_id and isinstance(n.foundation, (Issuer, SecIssuerRevision))
            for n in initial
        ):
            raise ServiceError(ServiceCode.NOT_FOUND)
    scoped = [EventScope(ScopeKind.COMPANY, (issuer_id,)), *broad]
    if security_id is not None:
        scoped.append(EventScope(ScopeKind.SECURITY, (security_id,)))
    if kind is ScopeKind.SYMBOL:
        scoped.append(request.scope)
    selected = tuple(sorted(publications.values(), key=lambda p: p.artifact_id))
    return (
        IdentityBinding(issuer_id, security_id, listing_id, tuple(p.artifact_id for p in selected)),
        tuple(sorted(set(scoped), key=lambda s: (s.kind.value, s.identifiers))),
        selected,
    )
