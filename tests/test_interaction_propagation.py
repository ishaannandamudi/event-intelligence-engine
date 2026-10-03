from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.graph.models import TraversalMode, TraversalPolicy
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import InteractionPolicy, PathReference, PropagationChain
from eie.interactions.queries import InteractionQueries
from eie.interactions.schema import CollisionType as C
from eie.interactions.validation import validate
from eie.persistence.codec import dumps
from examples.entity_relationship_graph import fixture_nodes, ingest, relationship
from examples.interaction_fixtures import MINUTE, query, retained_event
from tests.test_interaction_collisions import repo as repo


def graph_snapshot(repo, root, known, valid, items=None, depth=2, types=(R.SUPPLIER_TO,)):
    graph = GraphRepository(repo.store)
    nodes = fixture_nodes()
    for n in nodes:
        graph.append_node(n)
    if items is not None:
        ingest(graph, items, known=MINUTE)
    snapshot = traverse(
        graph,
        (root,),
        known,
        valid,
        TraversalPolicy(tuple(sorted(types)), max_depth=depth, mode=TraversalMode.SIMPLE_PATHS),
    )
    graph.append_snapshot(snapshot)
    return snapshot


def test_root_only_then_one_then_two_preserves_cutoff_bytes_and_paths(repo):
    a, b, c, *_ = fixture_nodes()
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    paths = (relationship("ab", R.SUPPLIER_TO, a, b), relationship("bc", R.SUPPLIER_TO, b, c))
    s1 = graph_snapshot(repo, a.node_id, 2 * MINUTE, 2 * MINUTE, paths)
    q1 = query(repo, root, 2 * MINUTE, graph_id=s1.snapshot_id)
    engine = InteractionEngine(repo)
    t1 = engine.interaction_context(q1)
    assert not t1.chronology_ids
    first = dumps(t1)
    retained_event(repo, "later-b", 4 * MINUTE, time=4 * MINUTE, entity=b.node_id)
    q2 = replace(q1, known_as_of_ns=4 * MINUTE)
    t2 = engine.interaction_context(q2)
    assert len(t2.chronology_ids) == 1
    second = dumps(t2)
    retained_event(repo, "later-c", 6 * MINUTE, time=6 * MINUTE, entity=c.node_id)
    t3 = engine.interaction_context(replace(q1, known_as_of_ns=6 * MINUTE))
    assert len(t3.chronology_ids) == 2
    entries = InteractionQueries(repo).propagation(replace(q1, known_as_of_ns=6 * MINUTE))
    assert {repo.artifact(e.path_id, PathReference).hop_count for e in entries} == {1, 2}
    assert all(e.description == "SUBSEQUENT_RELATED_EVENT" for e in entries)
    chain = repo.artifact(t3.chain_ids[0], PropagationChain)
    assert len(chain.layers) == 2 and not chain.truncation_reasons
    assert dumps(repo.context(t1.artifact_id)) == first
    assert dumps(repo.context(t2.artifact_id)) == second
    validate(repo, t3.artifact_id, deep=True)


@pytest.mark.parametrize("future,ended", [(True, False), (False, True)])
def test_future_and_terminated_root_edges_are_excluded(repo, future, ended):
    a, b, *_ = fixture_nodes()
    graph = GraphRepository(repo.store)
    for n in fixture_nodes():
        graph.append_node(n)
    edge = relationship("ab", R.SUPPLIER_TO, a, b, start=0)
    if ended:
        edge = replace(edge, valid_to_ns=MINUTE)
    ingest(graph, (edge,), known=5 * MINUTE if future else MINUTE)
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    retained_event(repo, "later", 3 * MINUTE, time=3 * MINUTE, entity=b.node_id)
    s = graph_snapshot(repo, a.node_id, 3 * MINUTE, 2 * MINUTE)
    ctx = InteractionEngine(repo).interaction_context(
        query(repo, root, 3 * MINUTE, graph_id=s.snapshot_id)
    )
    assert not ctx.chronology_ids and not ctx.path_ids


def test_bounded_cycle_two_hops_horizon_and_same_time_ties(repo):
    a, b, c, *_ = fixture_nodes()
    edges = (
        relationship("ab", R.SUPPLIER_TO, a, b),
        relationship("bc", R.SUPPLIER_TO, b, c),
        relationship("ca", R.SUPPLIER_TO, c, a),
    )
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    retained_event(repo, "same", 2 * MINUTE, time=2 * MINUTE, entity=b.node_id)
    retained_event(repo, "near", 3 * MINUTE, time=3 * MINUTE, entity=b.node_id)
    retained_event(repo, "near2", 3 * MINUTE, time=3 * MINUTE, entity=c.node_id)
    retained_event(repo, "far", 20 * MINUTE, time=20 * MINUTE, entity=b.node_id)
    s = graph_snapshot(repo, a.node_id, 20 * MINUTE, 2 * MINUTE, edges)
    p = InteractionPolicy(horizon_ns=2 * MINUTE, max_hops=2)
    q = query(repo, root, 20 * MINUTE, p, graph_id=s.snapshot_id)
    entries = InteractionQueries(repo).propagation(q)
    assert entries and all(e.elapsed_ns == MINUTE for e in entries)
    assert all(
        len(set(repo.artifact(e.path_id, PathReference).node_ids))
        == repo.artifact(e.path_id, PathReference).hop_count + 1
        for e in entries
    )
    ctx = InteractionEngine(repo).interaction_context(q)
    assert len(repo.artifact(ctx.chain_ids[0], PropagationChain).layers) == 1
    assert "TIED" in repo.artifact(ctx.chain_ids[0], PropagationChain).quality


def test_graph_related_and_historical_sector_collisions(repo):
    a, b, *rest = fixture_nodes()
    sector = rest[-2]
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    retained_event(repo, "b", 3 * MINUTE, time=3 * MINUTE, entity=b.node_id)
    retained_event(
        repo,
        "sector",
        3 * MINUTE,
        time=3 * MINUTE,
        entity=sector.node_id,
        scope=ScopeKind.SECTOR,
        family=EventFamily.OTHER,
        code="OTHER.SECTOR_DISCLOSURE",
    )
    s = graph_snapshot(
        repo,
        a.node_id,
        4 * MINUTE,
        2 * MINUTE,
        (
            relationship("ab", R.SUPPLIER_TO, a, b),
            relationship("sector", R.SECTOR_MEMBER_OF, a, sector),
        ),
        types=(R.SUPPLIER_TO, R.SECTOR_MEMBER_OF),
    )
    p = InteractionPolicy(relationship_types=tuple(sorted((R.SUPPLIER_TO, R.SECTOR_MEMBER_OF))))
    pairs = InteractionQueries(repo).collisions(
        query(repo, root, 4 * MINUTE, p, graph_id=s.snapshot_id)
    )
    assert len(pairs) == 2 and any(C.SECTOR_ISSUER in c.categories for c in pairs)
    assert all(C.GRAPH_RELATED in c.categories and c.path_ids for c in pairs)


@pytest.mark.parametrize(
    "known,valid,rootwrong",
    [
        (10 * MINUTE, 2 * MINUTE, False),
        (4 * MINUTE, 3 * MINUTE, False),
        (4 * MINUTE, 2 * MINUTE, True),
    ],
)
def test_future_wrong_valid_or_wrong_root_graph_rejected(repo, known, valid, rootwrong):
    a, b, *_ = fixture_nodes()
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    s = graph_snapshot(
        repo,
        b.node_id if rootwrong else a.node_id,
        known,
        valid,
        (relationship("ab", R.SUPPLIER_TO, a, b),),
    )
    with pytest.raises(ValidationError, match="graph"):
        InteractionEngine(repo).interaction_context(
            query(repo, root, 4 * MINUTE, graph_id=s.snapshot_id)
        )


def test_multiple_direct_parties_are_not_propagated(repo):
    from eie.events.scope import EventScope
    from eie.interactions.propagation import chronology

    a, b, *_ = fixture_nodes()
    root = retained_event(repo, "root", 2 * MINUTE, time=2 * MINUTE, entity=a.node_id)
    later = retained_event(repo, "later", 3 * MINUTE, time=3 * MINUTE, entity=b.node_id)
    s = graph_snapshot(
        repo, a.node_id, 4 * MINUTE, 2 * MINUTE, (relationship("ab", R.SUPPLIER_TO, a, b),)
    )
    q = query(repo, root, 4 * MINUTE, graph_id=s.snapshot_id)
    ctx = InteractionEngine(repo).interaction_context(q)
    rootrow = replace(
        repo.event(ctx.target_row_id),
        scope=EventScope(ScopeKind.COMPANY, tuple(sorted((a.node_id, b.node_id)))),
    )
    laterrow = repo.latest(rootrow.dataset_id, later.event_id, 4 * MINUTE)
    paths = tuple(repo.artifact(i, PathReference) for i in ctx.path_ids)
    assert not chronology(rootrow, (laterrow,), paths, InteractionPolicy(), 4 * MINUTE)[0]
