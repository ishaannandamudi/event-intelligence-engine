"""Composition snapshots: point-only defaults, explicit carry, partial and exhaustive."""

from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.graph.claims import Endpoint, GraphQuality, IdentifierKind, ValidSemantics
from eie.graph.exposure import ExposureDomain, ExposureMeasurement, ExposureType
from eie.graph.queries import GraphQueries
from eie.graph.taxonomy import GraphNodeType
from eie.graph.taxonomy import RelationshipType as R
from eie.providers.relationships.models import SnapshotPolicy, SnapshotScope
from tests.graph_helpers import endpoint, ingest, provider, relationship
from tests.graph_helpers import graph as graph


def holding(nodes, key, security, at, weight, *, point_only=False):
    etf = nodes[6]
    row = relationship(
        key,
        R.ETF_HOLDS,
        etf,
        security,
        at,
        ExposureMeasurement(ExposureType.PORTFOLIO_WEIGHT, at, "holdings-source", point=weight),
    )
    return replace(row, valid_semantics=ValidSemantics.SNAPSHOT) if point_only else row


def test_point_snapshot_does_not_persist_weight_forever(graph):
    repo, nodes = graph
    etf, sa = nodes[6], nodes[4]
    scope = SnapshotScope(endpoint(etf), R.ETF_HOLDS, 10, True)
    p = provider()
    first = ingest(
        repo, (holding(nodes, "a", sa, 10, 0.12, point_only=True),), 20, provider=p, snapshot=scope
    )
    assert GraphQueries(repo).etf_exposure(sa.node_id, 20, 10)[0].exposure.point == 0.12
    assert not GraphQueries(repo).etf_exposure(sa.node_id, 20, 11)
    ingest(repo, (), 30, provider=p, snapshot=replace(scope, effective_ns=25))
    assert GraphQueries(repo).etf_exposure(sa.node_id, 30, 10)
    assert len(repo.claim_history(repo.claim_revision(first.claim_revision_ids[0]).claim_id)) == 1


def test_partial_and_exhaustive_carry_weight_revisions_and_implied_removal(graph):
    repo, nodes = graph
    etf, sa, sb = nodes[6], nodes[4], nodes[5]
    p = provider(snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT, allow_implied_removal=True)
    scope = SnapshotScope(endpoint(etf), R.ETF_HOLDS, 10, True, True)
    ingest(
        repo,
        (holding(nodes, "a", sa, 10, 0.12), holding(nodes, "b", sb, 10, 0.08)),
        20,
        provider=p,
        snapshot=scope,
    )
    before = GraphQueries(repo).etf_exposure(etf.node_id, 20, 15)
    partial = replace(scope, effective_ns=30, exhaustive=False, remove_missing=False)
    ingest(repo, (holding(nodes, "a", sa, 30, 0.14),), 40, provider=p, snapshot=partial)
    now = GraphQueries(repo).etf_exposure(etf.node_id, 40, 35)
    assert len(now) == 2 and {e.exposure.point for e in now} == {0.14, 0.08}
    assert any(GraphQuality.PARTIAL_SNAPSHOT in e.quality for e in now)
    assert GraphQueries(repo).etf_exposure(etf.node_id, 20, 15) == before
    assert {e.exposure.point for e in GraphQueries(repo).etf_exposure(etf.node_id, 40, 15)} == {
        0.12,
        0.08,
    }
    full = replace(scope, effective_ns=50)
    receipt = ingest(repo, (holding(nodes, "a", sa, 50, 0.2),), 60, provider=p, snapshot=full)
    active = GraphQueries(repo).etf_exposure(etf.node_id, 60, 55)
    assert len(active) == 1 and active[0].exposure.point == 0.2
    assert len(receipt.claim_revision_ids) == 3
    assert any(
        GraphQuality.IMPLIED_TERMINATION in repo.claim_revision(i).quality
        for i in receipt.claim_revision_ids
    )
    assert len(GraphQueries(repo).etf_exposure(etf.node_id, 40, 55)) == 2
    assert len(GraphQueries(repo).etf_exposure(etf.node_id, 60, 35)) == 2


def test_exhaustive_index_membership_and_other_provider_is_preserved(graph):
    repo, nodes = graph
    sa, sb, index = nodes[4], nodes[5], nodes[7]
    p = provider(snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT, allow_implied_removal=True)
    scope = SnapshotScope(endpoint(index), R.INDEX_MEMBER_OF, 10, True, True)
    member = relationship(
        "member",
        R.INDEX_MEMBER_OF,
        sa,
        index,
        10,
        ExposureMeasurement(ExposureType.INDEX_WEIGHT, 10, "index-weight", point=0.1),
    )
    ingest(repo, (member,), 20, provider=p, snapshot=scope)
    ingest(
        repo,
        (replace(member, claim_key="independent"),),
        21,
        provider=provider("OTHER_INDEX"),
        resource="other-index",
    )
    ingest(
        repo,
        (relationship("member-b", R.INDEX_MEMBER_OF, sb, index, 30),),
        40,
        provider=p,
        snapshot=replace(scope, effective_ns=30),
    )
    current = GraphQueries(repo).index_memberships(index.node_id, 40, 35)
    assert len(current) == 2
    assert any(e.conflict_reasons for e in current)
    assert not GraphQueries(repo).relationships_as_of(
        40, 35, provider=p.source.name, node_id=sa.node_id, active_only=True
    )
    assert GraphQueries(repo).index_memberships(sa.node_id, 20, 10)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_permission",
        "future_scope",
        "wrong_effective",
        "wrong_owner",
        "unresolved_full",
        "signed_long_only",
        "same_valid_time",
    ],
)
def test_malformed_snapshot_refused_atomically(graph, failure):
    repo, nodes = graph
    etf, sa = nodes[6], nodes[4]
    p = provider(snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT, allow_implied_removal=True)
    scope = SnapshotScope(endpoint(etf), R.ETF_HOLDS, 10, True, True)
    row = holding(nodes, "a", sa, 10, 0.12)
    ingest(repo, (row,), 20, provider=p, snapshot=scope)
    old_claims = repo.current_claims(100)
    next_scope = replace(scope, effective_ns=30)
    next_row = holding(nodes, "a", sa, 30, 0.2)
    if failure == "missing_permission":
        p = replace(p, allow_implied_removal=False)
    elif failure == "future_scope":
        next_scope = replace(scope, effective_ns=100)
    elif failure == "wrong_effective":
        next_row = row
    elif failure == "wrong_owner":
        next_scope = replace(next_scope, owner=endpoint(nodes[7]))
    elif failure == "unresolved_full":
        next_row = replace(
            next_row,
            target_endpoint=Endpoint(
                IdentifierKind.PROVIDER_ID, "unknown-security", GraphNodeType.SECURITY, "AUDITED"
            ),
        )
    elif failure == "signed_long_only":
        next_row = replace(
            next_row,
            exposure=replace(
                next_row.exposure, point=-0.12, domain=ExposureDomain.SIGNED_PORTFOLIO
            ),
        )
    elif failure == "same_valid_time":
        next_scope, next_row = scope, row
    with pytest.raises(ValidationError):
        ingest(repo, (next_row,), 40, provider=p, snapshot=next_scope)
    assert repo.current_claims(100) == old_claims
    assert repo.last_batch(p.source.name, "relationships").checkpoint_generation == 1
    assert repo.store._rows("SELECT COUNT(*) FROM observations") == [(1,)]


def test_signed_portfolio_and_exhaustive_without_removal_policy(graph):
    repo, nodes = graph
    p = provider(snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT, long_only_portfolio=False)
    row = holding(nodes, "a", nodes[4], 10, 0.12)
    row = replace(
        row, exposure=replace(row.exposure, point=-0.12, domain=ExposureDomain.SIGNED_PORTFOLIO)
    )
    scope = SnapshotScope(endpoint(nodes[6]), R.ETF_HOLDS, 10, True)
    ingest(repo, (row,), 20, provider=p, snapshot=scope)
    ingest(repo, (), 40, provider=p, snapshot=replace(scope, effective_ns=30))
    assert GraphQueries(repo).etf_exposure(nodes[6].node_id, 40, 35)[0].exposure.point == -0.12
