"""Historical graph context is pinned; future edges and partial traversals cannot leak."""

from dataclasses import replace

import pytest

from eie.analogues.models import AnalogueFeature, AnaloguePolicy, ContextFeature
from eie.analogues.repository import AnalogueRepository
from eie.analogues.schema import FeatureKind, FeatureSource
from eie.core.errors import ValidationError
from eie.graph.nodes import NodeIdentifier, node_id
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.persistence.codec import dumps
from examples.intelligence_fixtures import batch, source
from tests.graph_helpers import graph, relationship, traversal
from tests.graph_helpers import ingest as graph_ingest
from tests.intelligence_helpers import compute, ingest

__all__ = ["graph"]


def test_future_sector_and_relationship_changes_preserve_old_analogue_features(graph):
    graph_repo, (issuer, other, *_) = graph
    sector, industry = graph[1][-2:]
    graph_ingest(
        graph_repo,
        (
            relationship("sector", R.SECTOR_MEMBER_OF, issuer, sector),
            relationship("industry", R.INDUSTRY_MEMBER_OF, issuer, industry),
        ),
        20,
    )
    snapshot = traverse(graph_repo, (issuer.node_id,), 30, 30, traversal())
    graph_repo.append_snapshot(snapshot)
    provider = source()
    item = batch(provider, 30, subject_id=issuer.node_id)
    ingest(graph_repo.store, provider, item)
    intel = compute(graph_repo.store, item)
    policy = AnaloguePolicy(
        features=(
            AnalogueFeature("sector", FeatureKind.CATEGORICAL, FeatureSource.CONTEXT, "sector"),
        )
    )
    repo = AnalogueRepository(graph_repo.store)
    repo.append_policy(policy)
    row = repo.index_features(
        intel.intelligence_id, policy.policy_id, graph_id=snapshot.snapshot_id
    )
    assert row.sector == sector.node_id and row.industry == industry.node_id
    assert row.values[0].value == sector.node_id
    assert "GRAPH_UNKNOWN_VALID_TO" in row.quality
    before = dumps(row)
    alternative = replace(
        sector,
        node_id=node_id(sector.node_type, "SYNTHETIC", "new-sector"),
        identifiers=(NodeIdentifier("SYNTHETIC", "new-sector"),),
        known_from_ns=40,
    )
    graph_repo.append_node(alternative)
    graph_ingest(
        graph_repo,
        (
            relationship("future-sector", R.SECTOR_MEMBER_OF, issuer, alternative),
            relationship("future-chain", R.SUPPLIER_TO, issuer, other),
        ),
        40,
    )
    future = traverse(graph_repo, (issuer.node_id,), 40, 30, traversal())
    graph_repo.append_snapshot(future)
    with pytest.raises(ValidationError, match="historical"):
        repo.index_features(intel.intelligence_id, policy.policy_id, graph_id=future.snapshot_id)
    assert dumps(repo.feature(row.row_id)) == before
    assert (
        repo.index_features(intel.intelligence_id, policy.policy_id, graph_id=snapshot.snapshot_id)
        == row
    )
    with pytest.raises(ValidationError, match="distinct"):
        repo.index_features(
            intel.intelligence_id,
            policy.policy_id,
            (ContextFeature("sector", "FORGED", "S", "1", 30, 30),),
            snapshot.snapshot_id,
        )


@pytest.mark.parametrize("case", ["missing", "wrong_valid", "partial"])
def test_graph_context_requires_exact_complete_historical_snapshot(graph, case):
    graph_repo, (issuer, other, *_) = graph
    provider = source()
    item = batch(provider, 30, subject_id=issuer.node_id)
    ingest(graph_repo.store, provider, item)
    intel = compute(graph_repo.store, item)
    repo = AnalogueRepository(graph_repo.store)
    policy = AnaloguePolicy()
    repo.append_policy(policy)
    if case == "missing":
        identity = "absent"
    else:
        graph_ingest(graph_repo, (relationship("s", R.SUPPLIER_TO, issuer, other),), 20)
        snapshot = traverse(
            graph_repo,
            (issuer.node_id,),
            30,
            20 if case == "wrong_valid" else 30,
            traversal(max_nodes=1) if case == "partial" else traversal(),
        )
        graph_repo.append_snapshot(snapshot)
        identity = snapshot.snapshot_id
    with pytest.raises(ValidationError):
        repo.index_features(intel.intelligence_id, policy.policy_id, graph_id=identity)


def test_null_sector_is_missing_context_not_string_category(graph):
    graph_repo, (issuer, *_) = graph
    provider = source()
    item = batch(provider, 30, subject_id=issuer.node_id)
    ingest(graph_repo.store, provider, item)
    intel = compute(graph_repo.store, item)
    repo = AnalogueRepository(graph_repo.store)
    policy = AnaloguePolicy()
    repo.append_policy(policy)
    row = repo.index_features(
        intel.intelligence_id,
        policy.policy_id,
        (ContextFeature("sector", None, "S", "1", 30, 30),),
    )
    assert row.sector is None
