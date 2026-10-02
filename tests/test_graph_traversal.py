"""Bounded paths, safe cycles, compatible exposure and pinned propagation."""

from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.events.scope import EventScope, ScopeKind
from eie.graph.exposure import ExposureMeasurement, ExposureType
from eie.graph.models import RelationshipPath, TraversalMode, snapshot_diff
from eie.graph.propagation import propagate
from eie.graph.taxonomy import Direction
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.graph.validation import validate_propagation, validate_snapshot
from eie.persistence import codec
from tests.graph_helpers import event, ingest, relationship, traversal
from tests.graph_helpers import graph as graph


def test_hierarchy_cycle_refusal_at_any_historical_valid_partition(graph):
    repo, (a, b, c, *_) = graph
    ingest(
        repo,
        (
            replace(relationship("a-b", R.PARENT_OF, a, b), valid_to_ns=30),
            relationship("b-c", R.PARENT_OF, b, c),
        ),
        20,
    )
    with pytest.raises(ValidationError, match="cycle"):
        ingest(repo, (relationship("c-a", R.PARENT_OF, c, a, 15),), 40)
    assert len(repo.current_claims(40)) == 2 and repo.store._rows(
        "SELECT COUNT(*) FROM observations"
    ) == [(1,)]
    ingest(repo, (relationship("b-a", R.PARENT_OF, b, a, 30),), 41)
    assert len(repo.current_claims(41)) == 3  # Adjacent half-open intervals do not overlap.


def test_supply_cycles_unique_nodes_and_distinct_paths(graph):
    repo, (a, b, c, d, *_) = graph
    ingest(
        repo,
        (
            relationship("a-b", R.SUPPLIER_TO, a, b),
            relationship("a-c", R.SUPPLIER_TO, a, c),
            relationship("b-d", R.SUPPLIER_TO, b, d),
            relationship("c-d", R.SUPPLIER_TO, c, d),
            relationship("d-a", R.SUPPLIER_TO, d, a),
        ),
        20,
    )
    unique = traverse(repo, (a.node_id,), 20, 10, traversal(R.SUPPLIER_TO, max_depth=8))
    assert len(unique.nodes) == 4 and len(unique.paths) == 3 and not unique.truncated
    paths = traverse(
        repo,
        (a.node_id,),
        20,
        10,
        traversal(
            R.SUPPLIER_TO,
            max_depth=8,
            mode=TraversalMode.SIMPLE_PATHS,
            direction=Direction.OUTGOING,
        ),
    )
    assert len([p for p in paths.paths if p.nodes[-1] == d.node_id]) == 2
    assert all(len(set(p.nodes)) == len(p.nodes) for p in paths.paths)
    assert validate_snapshot(paths, repo).valid
    assert traverse(repo, (a.node_id,), 20, 10, paths.traversal) == paths
    assert not traverse(repo, (a.node_id,), 19, 10, paths.traversal).edges
    assert not traverse(repo, (a.node_id,), 20, 9, paths.traversal).edges


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"max_nodes": 1}, "NODE_LIMIT"),
        ({"max_edges": 1}, "EDGE_LIMIT"),
        ({"max_paths": 1}, "PATH_LIMIT"),
        ({"adjacency_limit": 1}, "ADJACENCY_INPUT_LIMIT"),
        ({"max_result_bytes": 4096}, "RESULT_BYTES_LIMIT"),
    ],
)
def test_explicit_truncation_bounds(graph, kwargs, reason):
    repo, (a, b, c, *_) = graph
    ingest(
        repo, (relationship("a", R.SUPPLIER_TO, a, b), relationship("b", R.SUPPLIER_TO, a, c)), 20
    )
    limited = traverse(repo, (a.node_id,), 20, 10, traversal(R.SUPPLIER_TO, **kwargs))
    assert limited.truncated and limited.truncation_reasons == (reason,)
    assert validate_snapshot(limited, repo).valid
    zero = traverse(repo, (a.node_id,), 20, 10, traversal(R.SUPPLIER_TO, max_depth=0))
    assert len(zero.nodes) == 1 and not zero.paths and not zero.truncated
    with pytest.raises(ValidationError):
        traverse(repo, ("unresolved",), 20, 10, traversal())
    with pytest.raises(ValidationError):
        traverse(repo, (a.node_id, a.node_id), 20, 10, traversal())


def test_ownership_product_range_unknown_and_reverse_is_not_composed(graph):
    repo, (a, b, c, *_) = graph
    x = ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 10, "ownership", point=0.8)
    y = replace(x, point=0.5)
    ingest(
        repo,
        (
            relationship("a-b", R.PARENT_OF, a, b, exposure=x),
            relationship("b-c", R.OWNS_EQUITY_IN, b, c, exposure=y),
        ),
        20,
    )
    policy = traversal(
        R.PARENT_OF, R.OWNS_EQUITY_IN, derive_ownership=True, direction=Direction.OUTGOING
    )
    snapshot = traverse(repo, (a.node_id,), 20, 10, policy)
    long = next(p for p in snapshot.paths if p.depth == 2)
    assert long.derived_exposure.point == 0.4
    reverse = traverse(repo, (c.node_id,), 20, 10, replace(policy, direction=Direction.INCOMING))
    assert all(p.derived_exposure is None for p in reverse.paths)
    ingest(repo, (relationship("b-c", R.OWNS_EQUITY_IN, b, c),), 30)
    assert (
        next(
            p for p in traverse(repo, (a.node_id,), 30, 10, policy).paths if p.depth == 2
        ).derived_exposure
        is None
    )
    ingest(
        repo,
        (
            relationship(
                "b-c", R.OWNS_EQUITY_IN, b, c, exposure=replace(x, point=None, lower=0.4, upper=0.6)
            ),
        ),
        40,
    )
    ranged = next(
        p for p in traverse(repo, (a.node_id,), 40, 10, policy).paths if p.depth == 2
    ).derived_exposure
    assert (
        ranged.point is None
        and ranged.lower == pytest.approx(0.32)
        and ranged.upper == pytest.approx(0.48)
    )
    with pytest.raises(ValidationError):
        RelationshipPath((a.node_id, c.node_id), (long.edges[0],))
    with pytest.raises(ValidationError):
        RelationshipPath((a.node_id,), ())
    invalid_path = replace(long, derived_exposure=replace(long.derived_exposure, point=0.9))
    invalid = replace(
        snapshot,
        paths=tuple(
            sorted(
                (invalid_path, *[p for p in snapshot.paths if p != long]), key=lambda p: p.path_id
            )
        ),
    )
    assert not validate_snapshot(invalid).valid


def test_propagation_direct_one_two_hop_dedupe_filters_future_and_persistence(graph):
    repo, (a, b, c, *_) = graph
    ingest(
        repo,
        (
            relationship("a-b", R.SUPPLIER_TO, a, b),
            relationship("b-c", R.SUPPLIER_TO, b, c),
            relationship("a-c", R.PEER_OF, a, c),
        ),
        20,
    )
    source = event(a.node_id)
    policy = traversal(R.SUPPLIER_TO, max_depth=2, direction=Direction.OUTGOING)
    result = propagate(source, repo, 30, 25, policy)
    assert result.direct_entities == (a.node_id,)
    assert {x.candidate_entity for x in result.candidates} == {b.node_id, c.node_id}
    assert sorted(x.hop_count for x in result.candidates) == [1, 2]
    assert validate_propagation(result, repo).valid
    assert repo.append_propagation(result) and not repo.append_propagation(result)
    original_bytes = codec.dumps(result)
    ingest(repo, (relationship("a-c-new", R.SUPPLIER_TO, a, c, 10),), 40)
    assert codec.dumps(propagate(source, repo, 30, 25, policy)) == original_bytes
    assert codec.dumps(repo.propagation(result.result_id)) == original_bytes
    assert len(propagate(source, repo, 30, 25, replace(policy, max_depth=1)).candidates) == 1
    paths = propagate(source, repo, 40, 25, replace(policy, mode=TraversalMode.SIMPLE_PATHS))
    assert (
        len(paths.candidates) == 2
        and len(next(x for x in paths.candidates if x.candidate_entity == c.node_id).paths) == 2
    )
    assert snapshot_diff(result.snapshot, paths.snapshot).added
    assert snapshot_diff(paths.snapshot, result.snapshot).removed
    with pytest.raises(ValidationError):
        propagate(source, repo, 24, 25, policy)
    bad = replace(result, valid_at_ns=24)
    assert not validate_propagation(bad, repo).valid
    with pytest.raises(ValidationError):
        repo.append_propagation(bad)


@pytest.mark.parametrize(
    "scope",
    [
        EventScope(ScopeKind.MACRO_GLOBAL),
        EventScope(ScopeKind.MARKET),
        EventScope(ScopeKind.MACRO_COUNTRY, ("US",)),
        EventScope(ScopeKind.SEC_FILER, ("filer",)),
    ],
)
def test_broad_scopes_not_expanded(graph, scope):
    repo, nodes = graph
    original = event(nodes[0].node_id)
    broad = replace(original, identity=replace(original.identity, scope=scope))
    result = propagate(broad, repo, 30, 25, traversal())
    assert not result.candidates and result.snapshot is None and not result.direct_entities
    assert validate_propagation(result, repo).valid
    assert repo.append_propagation(result) and repo.propagation(result.result_id) == result
