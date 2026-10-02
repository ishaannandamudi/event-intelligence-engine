"""Explicit two-time relationship searches over indexed claim adjacency."""

from eie.core.time import require_ns
from eie.graph.exposure import ExposureType
from eie.graph.history import ClaimKnowledgeInterval, GraphHistoryQuery, GraphReader, resolve_edges
from eie.graph.models import DEFAULT_GRAPH_POLICY, GraphPolicy, RelationshipEdge
from eie.graph.taxonomy import SYMMETRIC, Direction, RelationshipType, canonical_type


class GraphQueries:
    def __init__(self, reader: GraphReader, policy: GraphPolicy = DEFAULT_GRAPH_POLICY) -> None:
        self.reader, self.policy = reader, policy

    def search_history(self, query: GraphHistoryQuery) -> tuple[ClaimKnowledgeInterval, ...]:
        return self.reader.search_history(query)

    def relationships_as_of(
        self,
        known_as_of_ns: int,
        valid_at_ns: int,
        *,
        node_id: str | None = None,
        relationship_types: tuple[RelationshipType, ...] = (),
        provider: str | None = None,
        exposure_type: ExposureType | None = None,
        active_only: bool = False,
        limit: int = 10000,
    ) -> tuple[RelationshipEdge, ...]:
        require_ns(known_as_of_ns)
        require_ns(valid_at_ns)
        kinds = tuple(sorted({canonical_type(k) for k in relationship_types}))
        claims = self.reader.current_claims(
            known_as_of_ns,
            node_id=node_id,
            relationship_types=kinds,
            provider=provider,
            limit=limit,
        )
        return tuple(
            e
            for e in resolve_edges(claims, known_as_of_ns, valid_at_ns, self.policy)
            if (not active_only or e.active)
            and (
                exposure_type is None
                or e.exposure is not None
                and e.exposure.measurement_type is exposure_type
            )
        )

    def neighbors(
        self,
        node_id: str,
        known_as_of_ns: int,
        valid_at_ns: int,
        *,
        relationship_types: tuple[RelationshipType, ...] = (),
        direction: Direction = Direction.BOTH,
        limit: int = 10000,
    ) -> tuple[RelationshipEdge, ...]:
        return tuple(
            e
            for e in self.relationships_as_of(
                known_as_of_ns,
                valid_at_ns,
                node_id=node_id,
                relationship_types=relationship_types,
                active_only=True,
                limit=limit,
            )
            if (
                e.relationship_type in SYMMETRIC
                or direction is Direction.BOTH
                or direction is Direction.OUTGOING
                and e.source_node_id == node_id
                or direction is Direction.INCOMING
                and e.target_node_id == node_id
            )
            and (not self.policy.exclude_conflicted_traversal or not e.conflict_reasons)
        )

    def parents(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.PARENT_OF,),
            direction=Direction.INCOMING,
        )

    def subsidiaries(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.PARENT_OF,),
            direction=Direction.OUTGOING,
        )

    def suppliers(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.SUPPLIER_TO,),
            direction=Direction.INCOMING,
        )

    def customers(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.SUPPLIER_TO,),
            direction=Direction.OUTGOING,
        )

    def competitors(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.COMPETITOR_OF, RelationshipType.PEER_OF),
        )

    def index_memberships(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id,
            known_as_of_ns,
            valid_at_ns,
            relationship_types=(RelationshipType.INDEX_MEMBER_OF,),
        )

    def etf_exposure(
        self, node_id: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[RelationshipEdge, ...]:
        return self.neighbors(
            node_id, known_as_of_ns, valid_at_ns, relationship_types=(RelationshipType.ETF_HOLDS,)
        )
