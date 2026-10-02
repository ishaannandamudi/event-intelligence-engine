"""Related entities for evaluation, with direct scope separate and no price prediction."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.entities.protocols import ResolutionState
from eie.entities.sec import SecEntityRegistry, SecListingRevision
from eie.events.models import CanonicalEvent
from eie.events.scope import ScopeKind
from eie.graph.history import GraphReader
from eie.graph.models import (
    DEFAULT_GRAPH_POLICY,
    GraphPolicy,
    GraphSnapshot,
    RelationshipPath,
    TraversalPolicy,
)
from eie.graph.nodes import GraphNode
from eie.graph.taxonomy import GraphNodeType
from eie.graph.traversal import traverse
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class EventRootBinding:
    scope_identifier: str
    node_id: str
    method: str
    exchange: str | None = None
    listing: GraphNode | None = None

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class PropagationCandidate:
    source_event_id: str
    source_revision_id: str
    candidate_entity: str
    graph_snapshot_id: str
    paths: tuple[RelationshipPath, ...]
    reason: str = "GRAPH_RELATED_ENTITY_FOR_EVALUATION"

    def __post_init__(self) -> None:
        contract(self)
        if not self.paths or any(p.nodes[-1] != self.candidate_entity for p in self.paths):
            raise ValidationError("candidate needs explicit related-entity paths")
        if tuple(p.path_id for p in self.paths) != tuple(sorted({p.path_id for p in self.paths})):
            raise ValidationError("ordered distinct candidate paths required")

    @property
    def hop_count(self) -> int:
        return min(p.depth for p in self.paths)


@dataclass(frozen=True, slots=True)
class PropagationResult:
    event: CanonicalEvent
    known_as_of_ns: int
    valid_at_ns: int
    bindings: tuple[EventRootBinding, ...]
    snapshot: GraphSnapshot | None
    candidates: tuple[PropagationCandidate, ...]
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if self.event.revision.available_from_ns > self.known_as_of_ns:
            raise ValidationError("future event cannot propagate historically")
        if tuple(c.candidate_entity for c in self.candidates) != tuple(
            sorted({c.candidate_entity for c in self.candidates})
        ):
            raise ValidationError("candidate entities must be ordered unique")
        from eie.persistence import codec

        if len(codec.dumps(self)) > 50_000_000:
            raise ValidationError("propagation exceeds serialized result bound")

    @property
    def direct_entities(self) -> tuple[str, ...]:
        return tuple(sorted({b.node_id for b in self.bindings}))

    @property
    def result_id(self) -> str:
        return "propagation_" + fingerprint("eie:graph-propagation:v1", self)


def event_roots(
    event: CanonicalEvent,
    reader: GraphReader,
    known_as_of_ns: int,
    valid_at_ns: int,
    registry: SecEntityRegistry | None = None,
    exchange: str | None = None,
) -> tuple[EventRootBinding, ...]:
    if event.revision.available_from_ns > known_as_of_ns:
        raise ValidationError("event unavailable at graph cutoff")
    scope = event.identity.scope
    allowed = {
        ScopeKind.COMPANY: (GraphNodeType.ISSUER,),
        ScopeKind.SECURITY: (GraphNodeType.SECURITY, GraphNodeType.ETF),
        ScopeKind.INDEX: (GraphNodeType.INDEX,),
        ScopeKind.SECTOR: (GraphNodeType.SECTOR,),
        ScopeKind.INDUSTRY: (GraphNodeType.INDUSTRY,),
    }
    if scope.kind in (
        ScopeKind.MARKET,
        ScopeKind.MACRO_GLOBAL,
        ScopeKind.MACRO_COUNTRY,
        ScopeKind.SEC_FILER,
    ):
        return ()
    bindings = []
    for identity in scope.identifiers:
        node_id, method = identity, "EXACT_SCOPE_NODE"
        listing = None
        if scope.kind is ScopeKind.SYMBOL:
            if exchange is None:
                raise ValidationError(
                    "symbol propagation requires exact historical listings and exchange"
                )
            listings = reader.symbol_listings(identity, exchange, known_as_of_ns, valid_at_ns)
            issuers = {
                n.foundation.issuer_id
                for n in listings
                if isinstance(n.foundation, SecListingRevision)
            }
            if len(issuers) != 1:
                raise ValidationError("unresolved or ambiguous historical event symbol")
            node_id, method = next(iter(issuers)), "EXACT_HISTORICAL_SYMBOL"
            listing = listings[0]
            if registry is not None:
                resolution = registry.resolve_symbol(
                    identity, exchange, known_as_of_ns, valid_at_ns=valid_at_ns
                )
                if resolution.state is not ResolutionState.RESOLVED or resolution.candidate_ids != (
                    node_id,
                ):
                    raise ValidationError("graph and existing registry symbol mapping disagree")
        node = reader.node(node_id, known_as_of_ns)
        kinds = (GraphNodeType.ISSUER,) if scope.kind is ScopeKind.SYMBOL else allowed[scope.kind]
        if node is None or node.node_type not in kinds:
            raise ValidationError("event scope requires known matching graph identity")
        bindings.append(
            EventRootBinding(
                identity,
                node_id,
                method,
                exchange if scope.kind is ScopeKind.SYMBOL else None,
                listing,
            )
        )
    return tuple(sorted(bindings, key=lambda b: (b.scope_identifier, b.node_id)))


def candidates_for(
    event: CanonicalEvent, snapshot: GraphSnapshot
) -> tuple[PropagationCandidate, ...]:
    groups: dict[str, list[RelationshipPath]] = {}
    for path in snapshot.paths:
        if path.nodes[-1] not in snapshot.roots:
            groups.setdefault(path.nodes[-1], []).append(path)
    return tuple(
        PropagationCandidate(
            event.event_id,
            event.revision_id,
            key,
            snapshot.snapshot_id,
            tuple(sorted(groups[key], key=lambda p: p.path_id)),
        )
        for key in sorted(groups)
    )


def propagate(
    event: CanonicalEvent,
    reader: GraphReader,
    known_as_of_ns: int,
    valid_at_ns: int,
    traversal: TraversalPolicy,
    policy: GraphPolicy = DEFAULT_GRAPH_POLICY,
    *,
    registry: SecEntityRegistry | None = None,
    exchange: str | None = None,
) -> PropagationResult:
    bindings = event_roots(event, reader, known_as_of_ns, valid_at_ns, registry, exchange)
    if not bindings:
        return PropagationResult(
            event, known_as_of_ns, valid_at_ns, (), None, (), ("SCOPE_NOT_AUTOMATICALLY_EXPANDED",)
        )
    roots = tuple(sorted({b.node_id for b in bindings}))
    snapshot = traverse(reader, roots, known_as_of_ns, valid_at_ns, traversal, policy)
    return PropagationResult(
        event,
        known_as_of_ns,
        valid_at_ns,
        bindings,
        snapshot,
        candidates_for(event, snapshot),
        snapshot.truncation_reasons,
    )
