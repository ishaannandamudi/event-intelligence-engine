"""Canonical sourced edge views, bounded traversal policies and frozen snapshots."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.graph import GRAPH_SCHEMA_VERSION
from eie.graph.claims import GraphQuality, ProviderRelationshipClaim, edge_identity
from eie.graph.exposure import DerivedPathExposure, ExposureMeasurement
from eie.graph.nodes import GraphNode
from eie.graph.taxonomy import Direction, RelationshipType, canonical_endpoints
from eie.sources.trust import SourcePrecedencePolicy
from eie.studies.checks import contract
from eie.version import __version__


@dataclass(frozen=True, slots=True)
class GraphPolicy:
    precedence: SourcePrecedencePolicy = SourcePrecedencePolicy()
    exclude_conflicted_traversal: bool = False
    stale_exposure_ns: int | None = None
    version: str = "relationship-resolution-1"

    def __post_init__(self) -> None:
        contract(self)


DEFAULT_GRAPH_POLICY = GraphPolicy()


@dataclass(frozen=True, slots=True)
class RelationshipEdge:
    relationship_type: RelationshipType
    source_node_id: str
    target_node_id: str
    qualifiers: tuple[tuple[str, str], ...]
    active: bool
    selected_claim: ProviderRelationshipClaim
    claims: tuple[ProviderRelationshipClaim, ...]
    confirmation_count: int
    conflict_reasons: tuple[str, ...]
    quality: frozenset[GraphQuality]
    policy: GraphPolicy

    def __post_init__(self) -> None:
        contract(self)
        if self.source_node_id == self.target_node_id or not self.claims:
            raise ValidationError("resolved distinct endpoints and claims required")
        endpoints = self.relationship_type, self.source_node_id, self.target_node_id
        if endpoints != canonical_endpoints(*endpoints):
            raise ValidationError("edge endpoints and direction must be canonical")
        if self.selected_claim not in self.claims or self.confirmation_count < 1:
            raise ValidationError(
                "selected sourced claim and descriptive confirmation count required"
            )
        if tuple(c.claim_id for c in self.claims) != tuple(
            sorted({c.claim_id for c in self.claims})
        ):
            raise ValidationError("canonical alternatives must be ordered distinct provider claims")
        if any(c.edge_id != self.edge_id for c in self.claims):
            raise ValidationError("canonical claims disagree on semantic edge identity")

    @property
    def edge_id(self) -> str:
        return edge_identity(
            self.relationship_type, self.source_node_id, self.target_node_id, self.qualifiers
        )

    @property
    def revision_id(self) -> str:
        return "edge_rev_" + fingerprint("eie:canonical-relationship-revision:v1", self)

    @property
    def exposure(self) -> ExposureMeasurement | None:
        return self.selected_claim.item.exposure if self.active else None

    @property
    def valid_from_ns(self) -> int | None:
        return self.selected_claim.item.valid_from_ns

    @property
    def valid_to_ns(self) -> int | None:
        return self.selected_claim.item.valid_to_ns

    @property
    def known_from_ns(self) -> int:
        return max(c.known_from_ns for c in self.claims)


class TraversalMode(StrEnum):
    UNIQUE_NODES = "UNIQUE_NODES"
    SIMPLE_PATHS = "SIMPLE_PATHS"


@dataclass(frozen=True, slots=True)
class TraversalPolicy:
    relationship_types: tuple[RelationshipType, ...]
    max_depth: int = 2
    direction: Direction = Direction.BOTH
    mode: TraversalMode = TraversalMode.UNIQUE_NODES
    max_nodes: int = 2048
    max_edges: int = 4096
    max_paths: int = 8192
    adjacency_limit: int = 10000
    derive_ownership: bool = False
    max_result_bytes: int = 20_000_000

    def __post_init__(self) -> None:
        contract(self)
        if not self.relationship_types or self.relationship_types != tuple(
            sorted(set(self.relationship_types))
        ):
            raise ValidationError("sorted distinct relationship filters required")
        if (
            not 0 <= self.max_depth <= 8
            or not 1 <= self.max_nodes <= 10000
            or not 1 <= self.max_edges <= 20000
            or not 1 <= self.max_paths <= 20000
            or not 1 <= self.adjacency_limit <= 100000
            or not 4096 <= self.max_result_bytes <= 50_000_000
        ):
            raise ValidationError("bounded depth/nodes/edges/paths/adjacency required")


@dataclass(frozen=True, slots=True)
class RelationshipPath:
    nodes: tuple[str, ...]
    edges: tuple[RelationshipEdge, ...]
    derived_exposure: DerivedPathExposure | None = None
    quality: frozenset[GraphQuality] = frozenset()

    def __post_init__(self) -> None:
        contract(self)
        if (
            not 1 <= len(self.edges) <= 8
            or len(self.nodes) != len(self.edges) + 1
            or len(set(self.nodes)) != len(self.nodes)
        ):
            raise ValidationError("bounded simple path requires distinct ordered nodes")
        for i, edge in enumerate(self.edges):
            if {self.nodes[i], self.nodes[i + 1]} != {edge.source_node_id, edge.target_node_id}:
                raise ValidationError("path endpoints disagree with edge")

    @property
    def depth(self) -> int:
        return len(self.edges)

    @property
    def path_id(self) -> str:
        return "path_" + fingerprint("eie:relationship-path:v1", self)


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    roots: tuple[str, ...]
    known_as_of_ns: int
    valid_at_ns: int
    traversal: TraversalPolicy
    policy: GraphPolicy
    nodes: tuple[GraphNode, ...]
    edges: tuple[RelationshipEdge, ...]
    paths: tuple[RelationshipPath, ...]
    truncation_reasons: tuple[str, ...] = ()
    schema_version: str = GRAPH_SCHEMA_VERSION
    engine_version: str = __version__

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.schema_version != GRAPH_SCHEMA_VERSION
            or self.roots != tuple(sorted(set(self.roots)))
            or not self.roots
        ):
            raise ValidationError("supported schema and ordered snapshot roots required")
        if (
            tuple(n.node_id for n in self.nodes) != tuple(sorted({n.node_id for n in self.nodes}))
            or tuple(e.edge_id for e in self.edges)
            != tuple(sorted({e.edge_id for e in self.edges}))
            or tuple(p.path_id for p in self.paths)
            != tuple(sorted({p.path_id for p in self.paths}))
        ):
            raise ValidationError("snapshot contents must be ordered distinct")
        if (
            len(self.nodes) > self.traversal.max_nodes
            or len(self.edges) > self.traversal.max_edges
            or len(self.paths) > self.traversal.max_paths
        ):
            raise ValidationError("snapshot exceeds declared bounds")
        from eie.persistence import codec

        if len(codec.dumps(self)) > self.traversal.max_result_bytes:
            raise ValidationError("snapshot exceeds declared serialized byte bound")

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_reasons)

    @property
    def snapshot_id(self) -> str:
        return "graph_snapshot_" + self.result_fingerprint

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:graph-snapshot:v1", self)


@dataclass(frozen=True, slots=True)
class GraphDiff:
    left_snapshot_id: str
    right_snapshot_id: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)


def snapshot_diff(left: GraphSnapshot, right: GraphSnapshot) -> GraphDiff:
    a, b = (
        {e.edge_id: e.revision_id for e in left.edges},
        {e.edge_id: e.revision_id for e in right.edges},
    )
    return GraphDiff(
        left.snapshot_id,
        right.snapshot_id,
        tuple(sorted(b.keys() - a.keys())),
        tuple(sorted(a.keys() - b.keys())),
        tuple(sorted(k for k in a.keys() & b.keys() if a[k] != b[k])),
    )
