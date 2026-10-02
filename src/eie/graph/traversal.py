"""Sorted bounded breadth-first traversal; simple paths never repeat a node."""

from collections import deque

from eie.core.errors import ValidationError
from eie.graph.claims import GraphQuality
from eie.graph.exposure import compose_ownership
from eie.graph.history import GraphLimitError, GraphReader
from eie.graph.models import (
    DEFAULT_GRAPH_POLICY,
    GraphPolicy,
    GraphSnapshot,
    RelationshipEdge,
    RelationshipPath,
    TraversalMode,
    TraversalPolicy,
)
from eie.graph.queries import GraphQueries
from eie.graph.taxonomy import RelationshipType


def make_path(
    nodes: tuple[str, ...], edges: tuple[RelationshipEdge, ...], derive: bool
) -> RelationshipPath:
    quality = {q for e in edges for q in e.quality}
    exposure = None
    if derive:
        # Only forward ownership paths compose. Reverse ownership is a different question.
        if any(
            e.relationship_type not in (RelationshipType.PARENT_OF, RelationshipType.OWNS_EQUITY_IN)
            or e.source_node_id != nodes[i]
            for i, e in enumerate(edges)
        ):
            quality.add(GraphQuality.EXPOSURE_UNAVAILABLE)
        else:
            try:
                exposure = compose_ownership(
                    tuple(e.exposure for e in edges), tuple(e.revision_id for e in edges)
                )
            except ValidationError:
                quality.add(GraphQuality.EXPOSURE_UNAVAILABLE)
            if exposure is None:
                quality.add(GraphQuality.EXPOSURE_UNAVAILABLE)
    return RelationshipPath(nodes, edges, exposure, frozenset(quality))


def traverse(
    reader: GraphReader,
    roots: tuple[str, ...],
    known_as_of_ns: int,
    valid_at_ns: int,
    traversal: TraversalPolicy,
    policy: GraphPolicy = DEFAULT_GRAPH_POLICY,
) -> GraphSnapshot:
    from eie.persistence import codec

    if not roots or roots != tuple(sorted(set(roots))) or len(roots) > traversal.max_nodes:
        raise ValidationError("sorted distinct roots within node bound required")
    nodes = {}
    for root in roots:
        node = reader.node(root, known_as_of_ns)
        if node is None:
            raise ValidationError("graph root unknown at selected cutoff")
        nodes[root] = node
    queries = GraphQueries(reader, policy)
    queue: deque[tuple[tuple[str, ...], tuple[RelationshipEdge, ...]]] = deque(
        ((r,), ()) for r in roots
    )
    edges: dict[str, RelationshipEdge] = {}
    paths: dict[str, RelationshipPath] = {}
    reasons = set()
    initial = GraphSnapshot(
        roots,
        known_as_of_ns,
        valid_at_ns,
        traversal,
        policy,
        tuple(nodes[k] for k in sorted(nodes)),
        (),
        (),
    )
    # Separate encoded components overestimate the enclosing JSON size. Reserve space
    # for truncation labels so a stopped traversal can still return its bounded result.
    used_bytes = len(codec.dumps(initial)) + 512
    if used_bytes > traversal.max_result_bytes:
        raise ValidationError("root evidence exceeds traversal byte budget")
    adjacency: dict[str, tuple[RelationshipEdge, ...]] = {}
    while queue:
        path_nodes, path_edges = queue.popleft()
        if len(path_edges) >= traversal.max_depth:
            continue
        tail = path_nodes[-1]
        if tail not in adjacency:
            try:
                adjacency[tail] = queries.neighbors(
                    tail,
                    known_as_of_ns,
                    valid_at_ns,
                    relationship_types=traversal.relationship_types,
                    direction=traversal.direction,
                    limit=traversal.adjacency_limit,
                )
            except GraphLimitError:
                reasons.add("ADJACENCY_INPUT_LIMIT")
                break
        for edge in adjacency[tail]:
            neighbor = edge.target_node_id if edge.source_node_id == tail else edge.source_node_id
            if neighbor in path_nodes:
                continue
            seen = neighbor in nodes
            if seen and traversal.mode is TraversalMode.UNIQUE_NODES:
                continue
            if not seen and len(nodes) >= traversal.max_nodes:
                reasons.add("NODE_LIMIT")
                break
            if edge.edge_id not in edges and len(edges) >= traversal.max_edges:
                reasons.add("EDGE_LIMIT")
                break
            if len(paths) >= traversal.max_paths:
                reasons.add("PATH_LIMIT")
                break
            node = reader.node(neighbor, known_as_of_ns)
            if node is None:
                raise ValidationError("canonical endpoint missing at historical cutoff")
            new_nodes, new_edges = (*path_nodes, neighbor), (*path_edges, edge)
            path = make_path(new_nodes, new_edges, traversal.derive_ownership)
            extra_bytes = (
                (len(codec.dumps(node)) if not seen else 0)
                + (len(codec.dumps(edge)) if edge.edge_id not in edges else 0)
                + len(codec.dumps(path))
                + 3
            )
            if used_bytes + extra_bytes > traversal.max_result_bytes:
                reasons.add("RESULT_BYTES_LIMIT")
                break
            used_bytes += extra_bytes
            nodes[neighbor] = node
            edges[edge.edge_id] = edge
            paths[path.path_id] = path
            queue.append((new_nodes, new_edges))
        if reasons:
            break
    return GraphSnapshot(
        roots,
        known_as_of_ns,
        valid_at_ns,
        traversal,
        policy,
        tuple(nodes[k] for k in sorted(nodes)),
        tuple(edges[k] for k in sorted(edges)),
        tuple(paths[k] for k in sorted(paths)),
        tuple(sorted(reasons)),
    )
