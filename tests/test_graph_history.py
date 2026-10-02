"""Two-time causality, source alternatives, corrections and every relationship family."""

from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.graph.claims import (
    ClaimRevisionKind,
    ClaimState,
    Endpoint,
    GraphQuality,
    IdentifierKind,
    ValidSemantics,
)
from eie.graph.engine import GraphIngestor
from eie.graph.exposure import ExposureMeasurement, ExposureType
from eie.graph.history import GraphLimitError, knowledge_intervals
from eie.graph.models import GraphPolicy
from eie.graph.nodes import NodeIdentifier
from eie.graph.queries import GraphQueries
from eie.graph.taxonomy import Direction, GraphNodeType
from eie.graph.taxonomy import RelationshipType as R
from eie.persistence import codec
from eie.sources.trust import SourceClass, SourcePrecedencePolicy
from tests.graph_helpers import graph as graph
from tests.graph_helpers import ingest, provider, relationship


def test_valid_knowledge_retroactive_correction_and_termination(graph):
    repo, (a, b, *_) = graph
    row = relationship("supply", R.SUPPLIER_TO, a, b, 10)
    first = ingest(repo, (row,), 100)
    queries = GraphQueries(repo)
    assert not queries.customers(a.node_id, 99, 50)
    assert not queries.customers(a.node_id, 100, 9)
    before = codec.dumps(queries.customers(a.node_id, 100, 50))
    claim = repo.claim_revision(first.claim_revision_ids[0])
    assert claim is not None
    ingest(repo, (replace(row, valid_to_ns=40, revision_kind=ClaimRevisionKind.CORRECTION),), 200)
    assert codec.dumps(queries.customers(a.node_id, 100, 50)) == before
    assert queries.customers(a.node_id, 200, 39)
    assert not queries.customers(a.node_id, 200, 40)
    ended = queries.relationships_as_of(200, 50)
    assert len(ended) == 1 and not ended[0].active
    ingest(repo, (replace(row, valid_to_ns=30, revision_kind=ClaimRevisionKind.TERMINATION),), 300)
    history = repo.relationship_history(claim.edge_id, 300)
    assert [c.number for c in history] == [1, 2, 3]
    windows = knowledge_intervals(history, 200)
    assert [w.known_to_ns for w in windows] == [200, None]
    assert len(repo.relationship_history(claim.edge_id, 199)) == 1
    assert queries.suppliers(b.node_id, 100, 50)
    with pytest.raises(ValidationError):
        ingest(repo, (row,), 250)
    with pytest.raises(ValidationError):
        ingest(repo, (replace(row, target_endpoint=row.source_endpoint),), 400)


def test_unknown_validity_unresolved_claim_and_future_node(graph):
    repo, (a, b, *_) = graph
    unknown = replace(
        relationship("unknown", R.PEER_OF, a, b),
        valid_from_ns=None,
        valid_semantics=ValidSemantics.UNKNOWN,
    )
    missing = replace(
        relationship("missing", R.SUPPLIER_TO, a, b),
        target_endpoint=Endpoint(
            IdentifierKind.PROVIDER_ID, "unmapped", GraphNodeType.ISSUER, "AUDITED_PROVIDER"
        ),
    )
    result = ingest(repo, (unknown, missing), 20)
    assert result.unresolved_count == 1
    assert not GraphQueries(repo).relationships_as_of(20, 10)
    claims = repo.current_claims(20)
    assert any(GraphQuality.UNKNOWN_VALID_FROM in c.quality for c in claims)
    assert any(GraphQuality.UNRESOLVED_ENTITY in c.quality for c in claims)
    repo.append_node(
        replace(
            b,
            identifiers=tuple(
                sorted(
                    (*b.identifiers, NodeIdentifier("AUDITED_PROVIDER", "unmapped")),
                    key=lambda i: (i.authority, i.value),
                )
            ),
            known_from_ns=30,
        )
    )
    result2 = ingest(repo, (replace(missing, revision_kind=ClaimRevisionKind.UPDATE),), 40)
    assert result2.unresolved_count == 0
    assert repo.current_claims(20) == claims
    assert GraphQueries(repo).customers(a.node_id, 40, 10)


def test_source_conflicts_precedence_confirmation_and_staleness(graph):
    repo, (a, b, *_) = graph
    value = ExposureMeasurement(ExposureType.REVENUE_SHARE, 10, "primary", point=0.12)
    row = relationship("supply", R.SUPPLIER_TO, a, b, exposure=value)
    ingest(repo, (row,), 20)
    ingest(
        repo,
        (
            replace(
                row, claim_key="other", valid_to_ns=15, revision_kind=ClaimRevisionKind.TERMINATION
            ),
        ),
        30,
        provider=provider(),
    )
    edge = GraphQueries(repo).customers(a.node_id, 30, 25)[0]
    assert edge.active and edge.exposure.point == 0.12 and len(edge.claims) == 2
    assert "SOURCE_STATE_OR_EXPOSURE_DISAGREEMENT" in edge.conflict_reasons
    assert GraphQuality.CONFLICTING_SOURCE in edge.quality
    assert not GraphQueries(repo, GraphPolicy(exclude_conflicted_traversal=True)).customers(
        a.node_id, 30, 25
    )
    reversed_order = tuple(reversed(SourcePrecedencePolicy().baseline))
    weak_first = GraphPolicy(precedence=SourcePrecedencePolicy(reversed_order))
    assert not GraphQueries(repo, weak_first).customers(a.node_id, 30, 25)
    ingest(
        repo,
        (
            replace(
                row, claim_key="third", exposure=replace(value, source_reference="corroborating")
            ),
        ),
        31,
        provider=provider("REL_C", SourceClass.COMPANY_PRIMARY),
    )
    confirmed = GraphQueries(repo, GraphPolicy(stale_exposure_ns=10)).customers(a.node_id, 31, 25)[
        0
    ]
    assert confirmed.confirmation_count == 2 and GraphQuality.STALE_EXPOSURE in confirmed.quality
    ingest(
        repo,
        (replace(row, claim_key="fourth", exposure=replace(value, point=0.2, estimated=True)),),
        32,
        provider=provider("REL_D", SourceClass.COMPANY_PRIMARY),
    )
    assert (
        "EQUAL_PRECEDENCE_DISAGREEMENT"
        in GraphQueries(repo).customers(a.node_id, 32, 25)[0].conflict_reasons
    )
    only = GraphQueries(repo).relationships_as_of(
        32, 25, provider="REL_D", exposure_type=ExposureType.REVENUE_SHARE
    )[0]
    assert GraphQuality.ESTIMATED_VALUE in only.quality
    assert not GraphQueries(repo).relationships_as_of(
        32, 25, exposure_type=ExposureType.OWNERSHIP_PERCENT
    )


@pytest.mark.parametrize(
    "kind,source,target",
    [
        (R.PARENT_OF, 0, 1),
        (R.SUBSIDIARY_OF, 1, 0),
        (R.SUPPLIER_TO, 0, 1),
        (R.CUSTOMER_OF, 1, 0),
        (R.COMPETITOR_OF, 0, 1),
        (R.PEER_OF, 0, 1),
        (R.SECTOR_MEMBER_OF, 0, 8),
        (R.INDUSTRY_MEMBER_OF, 0, 9),
        (R.INDEX_MEMBER_OF, 4, 7),
        (R.ETF_HOLDS, 6, 4),
        (R.OWNS_EQUITY_IN, 0, 1),
        (R.OWNED_BY, 1, 0),
        (R.SPINOFF_FROM, 1, 0),
        (R.SUCCESSOR_OF, 1, 0),
        (R.PREDECESSOR_OF, 0, 1),
        (R.ISSUER_ISSUES_SECURITY, 0, 4),
    ],
)
def test_relationship_taxonomy_and_queries(graph, kind, source, target):
    repo, nodes = graph
    ingest(repo, (relationship("family", kind, nodes[source], nodes[target]),), 20)
    edges = GraphQueries(repo).relationships_as_of(20, 10, relationship_types=(kind,))
    assert len(edges) == 1 and edges[0].active
    q = GraphQueries(repo)
    assert q.neighbors(nodes[source].node_id, 20, 10)
    if kind is R.SUBSIDIARY_OF:
        assert q.parents(nodes[1].node_id, 20, 10) and q.subsidiaries(nodes[0].node_id, 20, 10)
    if kind is R.COMPETITOR_OF:
        assert q.competitors(nodes[1].node_id, 20, 10)
    if kind is R.ETF_HOLDS:
        assert q.etf_exposure(nodes[4].node_id, 20, 10)
    if kind is R.INDEX_MEMBER_OF:
        assert q.index_memberships(nodes[4].node_id, 20, 10)


def test_directional_queries_and_prior_data_bounds(graph):
    repo, (a, b, c, *_) = graph
    ingest(
        repo, (relationship("a", R.SUPPLIER_TO, a, b), relationship("b", R.SUPPLIER_TO, a, c)), 20
    )
    q = GraphQueries(repo)
    assert not q.neighbors(a.node_id, 20, 10, direction=Direction.INCOMING)
    assert len(q.neighbors(a.node_id, 20, 10, direction=Direction.OUTGOING)) == 2
    with pytest.raises(GraphLimitError):
        repo.current_claims(20, node_id=a.node_id, limit=1)
    with pytest.raises(ValidationError):
        repo.current_claims(20, limit=True)
    with pytest.raises(ValidationError):
        q.relationships_as_of(-1, 10)
    assert not repo.current_claims(19)


def test_cik_exact_provider_mapping_no_fuzzy_and_retracted(graph):
    repo, (a, b, *_) = graph
    engine = GraphIngestor(repo)
    assert (
        engine.resolve(Endpoint(IdentifierKind.CIK, a.foundation.cik, GraphNodeType.ISSUER), 20, 10)
        == a.node_id
    )
    assert (
        engine.resolve(Endpoint(IdentifierKind.NODE_ID, a.node_id, GraphNodeType.INDEX), 20, 10)
        is None
    )
    assert (
        engine.resolve(
            Endpoint(IdentifierKind.SYMBOL, "UNKNOWN", GraphNodeType.ISSUER, exchange="XNAS"),
            20,
            10,
        )
        is None
    )
    row = relationship("peer", R.PEER_OF, a, b)
    ingest(repo, (row,), 20)
    ingest(
        repo,
        (replace(row, state=ClaimState.RETRACTED, revision_kind=ClaimRevisionKind.CORRECTION),),
        30,
    )
    assert GraphQueries(repo).competitors(a.node_id, 20, 10)
    assert not GraphQueries(repo).competitors(a.node_id, 30, 10)
