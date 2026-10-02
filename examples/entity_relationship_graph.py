"""Offline sourced graph, two-time correction replay, paths and candidate restart."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.quality import ConfirmationState
from eie.entities.models import Issuer, Security, security_id
from eie.entities.sec import sec_issuer_id
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.graph.claims import ClaimRevisionKind, Endpoint, IdentifierKind, RelationshipItem
from eie.graph.engine import GraphIngestor
from eie.graph.exposure import ExposureMeasurement, ExposureType
from eie.graph.models import GraphSnapshot, TraversalMode, TraversalPolicy
from eie.graph.nodes import GraphDataset, GraphNode, NodeIdentifier, foundation_node, node_id
from eie.graph.propagation import propagate
from eie.graph.queries import GraphQueries
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import GraphNodeType, RelationshipType
from eie.graph.traversal import traverse
from eie.graph.validation import validate_propagation, validate_snapshot
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.relationships.adapter import RelationshipAdapter
from eie.providers.relationships.models import (
    RelationshipBatch,
    RelationshipProvider,
    SnapshotPolicy,
    SnapshotScope,
)
from eie.providers.sec.models import CIK
from eie.sources.models import SourceIdentity
from eie.sources.trust import SourceClass

SOURCE = SourceIdentity("SYNTHETIC_REL", "1", "structured-1", SourceClass.COMPANY_PRIMARY)
DATASET = GraphDataset("synthetic-relationships", sha256(b"eie-10-example").hexdigest(), "1")


def fixture_nodes() -> tuple[GraphNode, ...]:
    issuers = tuple(
        Issuer(sec_issuer_id(CIK(f"{j:010d}")), f"Synthetic issuer {j}", f"{j:010d}")
        for j in range(1, 5)
    )
    securities = tuple(
        Security(
            security_id("FIXTURE_SECURITY", str(j)),
            issuers[j - 1].issuer_id,
            f"Synthetic security {j}",
        )
        for j in range(1, 3)
    )
    result = [foundation_node(v, SOURCE, DATASET, 0) for v in (*issuers, *securities)]
    for kind, identifier in (
        (GraphNodeType.ETF, "fund-1"),
        (GraphNodeType.INDEX, "index-1"),
        (GraphNodeType.SECTOR, "taxonomy-v1-sector-1"),
        (GraphNodeType.INDUSTRY, "taxonomy-v1-industry-1"),
    ):
        result.append(
            GraphNode(
                node_id(kind, "SYNTHETIC", identifier),
                kind,
                identifier,
                0,
                SOURCE,
                DATASET,
                (NodeIdentifier("SYNTHETIC", identifier),),
            )
        )
    return tuple(result)


def endpoint(node: GraphNode) -> Endpoint:
    return Endpoint(IdentifierKind.NODE_ID, node.node_id, node.node_type)


def relationship(
    key: str,
    kind: RelationshipType,
    source: GraphNode,
    target: GraphNode,
    start: int = 10,
    exposure: ExposureMeasurement | None = None,
) -> RelationshipItem:
    return RelationshipItem(key, kind, endpoint(source), endpoint(target), start, exposure=exposure)


def event(issuer: str, known: int = 25) -> CanonicalEvent:
    return CanonicalEvent(
        EventIdentity(
            EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE"),
            EventScope(ScopeKind.COMPANY, (issuer,)),
            "synthetic-graph-event",
        ),
        EventRevision(1, RevisionKind.ORIGINAL, known),
        LifecycleState.RELEASED,
        ConfirmationState.CONFIRMED_PRIMARY,
        ("obs_" + "0" * 64,),
        frozenset({SourceClass.COMPANY_PRIMARY}),
        "Synthetic issuer event",
        published_at_ns=known,
    )


def ingest(
    repository: GraphRepository,
    items: tuple[RelationshipItem, ...],
    known: int,
    *,
    provider: RelationshipProvider | None = None,
    resource: str = "relationships",
    snapshot: SnapshotScope | None = None,
):
    p = provider or RelationshipProvider(SOURCE, DATASET, frozenset(RelationshipType))
    batch = RelationshipBatch(
        resource,
        f"synthetic-item-{known}",
        str(known),
        known,
        known,
        tuple(sorted(items, key=lambda i: i.claim_key)),
        snapshot=snapshot,
    )
    return GraphIngestor(repository).ingest(RelationshipAdapter(p), codec.dumps(batch))


def demo(root: Path) -> dict[str, object]:
    nodes = fixture_nodes()
    a, b, c, _, sa, sb, etf, index, sector, industry = nodes

    def ownership(value: float) -> ExposureMeasurement:
        return ExposureMeasurement(
            ExposureType.OWNERSHIP_PERCENT, 10, "synthetic-disclosure", point=value
        )

    with SQLiteEventStore(root / "graph.sqlite") as store:
        repo = GraphRepository(store)
        for n in nodes:
            repo.append_node(n)
        supplier = relationship("supply-a-b", RelationshipType.SUPPLIER_TO, a, b)
        ingest(
            repo,
            (
                supplier,
                relationship(
                    "parent-a-b", RelationshipType.PARENT_OF, a, b, exposure=ownership(0.8)
                ),
                relationship(
                    "parent-b-c", RelationshipType.PARENT_OF, b, c, exposure=ownership(0.5)
                ),
                relationship("issuer-security", RelationshipType.ISSUER_ISSUES_SECURITY, a, sa),
                relationship("sector", RelationshipType.SECTOR_MEMBER_OF, a, sector),
                relationship("industry", RelationshipType.INDUSTRY_MEMBER_OF, a, industry),
            ),
            20,
        )
        provider = RelationshipProvider(
            SOURCE,
            DATASET,
            frozenset(RelationshipType),
            snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT,
            allow_implied_removal=True,
        )
        holding = relationship(
            "holding-a",
            RelationshipType.ETF_HOLDS,
            etf,
            sa,
            exposure=ExposureMeasurement(
                ExposureType.PORTFOLIO_WEIGHT, 10, "synthetic-holdings", point=0.12
            ),
        )
        ingest(
            repo,
            (holding,),
            21,
            provider=provider,
            resource="holdings",
            snapshot=SnapshotScope(endpoint(etf), RelationshipType.ETF_HOLDS, 10, True, True),
        )
        membership = relationship("member-a", RelationshipType.INDEX_MEMBER_OF, sa, index)
        ingest(
            repo,
            (membership,),
            22,
            provider=provider,
            resource="index",
            snapshot=SnapshotScope(
                endpoint(index), RelationshipType.INDEX_MEMBER_OF, 10, True, True
            ),
        )
        policy = TraversalPolicy(
            tuple(sorted(RelationshipType)),
            max_depth=2,
            mode=TraversalMode.SIMPLE_PATHS,
            derive_ownership=True,
        )
        before = traverse(repo, (a.node_id,), 30, 25, policy)
        stored_bytes = codec.dumps(before)
        correction = replace(supplier, valid_to_ns=15, revision_kind=ClaimRevisionKind.CORRECTION)
        ingest(repo, (correction,), 40)
        assert codec.dumps(traverse(repo, (a.node_id,), 30, 25, policy)) == stored_bytes
        assert GraphQueries(repo).customers(a.node_id, 30, 25)
        assert not GraphQueries(repo).customers(a.node_id, 40, 25)
        repo.append_snapshot(before)
        propagation = propagate(event(a.node_id), repo, 30, 25, policy)
        repo.append_propagation(propagation)
        assert (
            validate_snapshot(before, repo).valid and validate_propagation(propagation, repo).valid
        )
        ownership_paths = tuple(
            p
            for p in before.paths
            if p.nodes == (a.node_id, b.node_id, c.node_id) and p.derived_exposure is not None
        )
        assert ownership_paths and ownership_paths[0].derived_exposure is not None
        derived = ownership_paths[0].derived_exposure.point
        assert derived == 0.4
        snapshot_id, result_id = before.snapshot_id, propagation.result_id
    with SQLiteEventStore(root / "graph.sqlite") as restarted:
        repo = GraphRepository(restarted)
        loaded: GraphSnapshot | None = repo.snapshot(snapshot_id)
        assert loaded is not None and codec.dumps(loaded) == stored_bytes
        assert repo.propagation(result_id) == propagation
    return {
        "known_as_of": 30,
        "valid_at": 25,
        "snapshot_fingerprint": before.result_fingerprint,
        "nodes": len(before.nodes),
        "edges": len(before.edges),
        "paths": len(before.paths),
        "derived_ownership": derived,
        "propagation_candidates": len(propagation.candidates),
        "historical_snapshot_unchanged": True,
        "restart_byte_identical": True,
        "interpretation": "Sourced relationships and candidates for evaluation; "
        "no predicted movement or trading direction.",
    }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), sort_keys=True))
