"""Historical ledger sealing, interval searches and independent artifact tamper checks."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.entities.sec import SecEntityRegistry, SecIssuerRevision
from eie.graph.claims import ClaimRevisionKind, Endpoint, IdentifierKind, ValidSemantics
from eie.graph.engine import GraphIngestor
from eie.graph.exposure import (
    DerivedPathExposure,
    ExposureMeasurement,
    ExposureType,
    compose_ownership,
)
from eie.graph.history import ClaimKnowledgeInterval, GraphHistoryQuery, GraphLimitError
from eie.graph.models import RelationshipPath, snapshot_diff
from eie.graph.nodes import NodeIdentifier, foundation_node, node_id
from eie.graph.propagation import propagate
from eie.graph.queries import GraphQueries
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import Direction, GraphNodeType
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.graph.validation import (
    validate_claim_history,
    validate_ingestion,
    validate_propagation,
    validate_snapshot,
)
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.relationships.adapter import RelationshipAdapter
from eie.providers.relationships.models import SnapshotPolicy, SnapshotScope
from eie.providers.sec.models import CIK
from tests.graph_helpers import (
    DATASET,
    SOURCE,
    endpoint,
    event,
    ingest,
    provider,
    relationship,
    traversal,
)
from tests.graph_helpers import graph as graph
from tests.test_graph_identity import listings


def test_interval_search_has_two_independent_clocks_and_no_future_boundary(graph):
    repo, (a, b, *_) = graph
    weight = ExposureMeasurement(ExposureType.REVENUE_SHARE, 10, "audited", point=0.12)
    first = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b, exposure=weight),), 20)
    claim = repo.claim_revision(first.claim_revision_ids[0])
    ingest(
        repo, (replace(claim.item, valid_to_ns=25, revision_kind=ClaimRevisionKind.CORRECTION),), 40
    )
    q = GraphQueries(repo)
    prior = q.search_history(GraphHistoryQuery(30, node_id=a.node_id))
    assert len(prior) == 1 and prior[0].known_to_ns is None
    both = q.search_history(GraphHistoryQuery(50, known_interval=(20, 50)))
    assert [(r.claim.number, r.known_to_ns) for r in both] == [(1, 40), (2, None)]
    old = q.search_history(GraphHistoryQuery(50, known_interval=(30, 40)))
    assert len(old) == 1 and old[0].claim.number == 1
    corrected = q.search_history(
        GraphHistoryQuery(
            50,
            node_id=b.node_id,
            provider=SOURCE.name,
            relationship_types=(R.CUSTOMER_OF,),
            exposure_type=ExposureType.REVENUE_SHARE,
            known_interval=(40, 50),
            valid_interval=(24, 25),
        )
    )
    assert len(corrected) == 1 and corrected[0].claim.number == 2
    assert not q.search_history(
        GraphHistoryQuery(50, known_interval=(40, 50), valid_interval=(25, 26))
    )
    assert not q.search_history(GraphHistoryQuery(50, provider="unknown"))
    with pytest.raises(GraphLimitError):
        q.search_history(GraphHistoryQuery(50, limit=1))
    with pytest.raises(GraphLimitError):
        repo.claim_history(claim.claim_id, limit=1)
    with pytest.raises(GraphLimitError):
        repo.relationship_history(claim.edge_id, 50, limit=1)
    with pytest.raises(ValidationError):
        ClaimKnowledgeInterval(claim, 20)


def test_snapshot_and_unknown_validity_interval_search(graph):
    repo, (a, b, *_) = graph
    snapshot = replace(
        relationship("point", R.PEER_OF, a, b), valid_semantics=ValidSemantics.SNAPSHOT
    )
    unknown = replace(
        relationship("unknown", R.SUPPLIER_TO, a, b),
        valid_from_ns=None,
        valid_semantics=ValidSemantics.UNKNOWN,
    )
    ingest(repo, (snapshot, unknown), 20)
    assert len(repo.search_history(GraphHistoryQuery(20))) == 2
    assert len(repo.search_history(GraphHistoryQuery(20, valid_interval=(10, 11)))) == 1
    assert not repo.search_history(GraphHistoryQuery(20, valid_interval=(11, 12)))


def test_unresolved_inverse_claim_remains_searchable_by_canonical_taxonomy(graph):
    repo, (a, b, *_) = graph
    row = replace(
        relationship("unknown-parent", R.SUBSIDIARY_OF, a, b),
        target_endpoint=Endpoint(
            IdentifierKind.PROVIDER_ID, "unmapped", GraphNodeType.ISSUER, "AUDITED"
        ),
    )
    ingest(repo, (row,), 20)
    claims = repo.current_claims(20, node_id=a.node_id, relationship_types=(R.PARENT_OF,))
    assert len(claims) == 1 and claims[0].endpoints == (R.PARENT_OF, None, a.node_id)
    assert (
        repo.search_history(GraphHistoryQuery(20, relationship_types=(R.SUBSIDIARY_OF,)))[0].claim
        == claims[0]
    )
    assert not GraphQueries(repo).parents(a.node_id, 20, 10)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"known_interval": (10, 10)},
        {"valid_interval": (20, 10)},
        {"valid_interval": (-1, 10)},
        {"limit": 0},
        {"limit": True},
        {"relationship_types": (R.PEER_OF, R.PEER_OF)},
        {"provider": " bad"},
    ],
)
def test_history_query_refuses_malformed_filters(kwargs):
    with pytest.raises(ValidationError):
        GraphHistoryQuery(20, **kwargs)


def test_sealed_history_refuses_backdated_nodes_claims_and_empty_checkpoints(graph):
    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    before = traverse(repo, (a.node_id,), 30, 10, traversal())
    repo.append_snapshot(before)
    for known in (19, 20, 30):
        with pytest.raises(ValidationError):
            ingest(repo, (relationship("new", R.PEER_OF, a, b),), known, resource="new")
        with pytest.raises(ValidationError):
            ingest(repo, (), known, resource="empty")
        with pytest.raises(ValidationError):
            repo.append_node(replace(a, known_from_ns=known, label="backdated label"))
    ingest(repo, (relationship("new", R.PEER_OF, a, b),), 31)
    assert traverse(repo, (a.node_id,), 30, 10, traversal()) == before
    assert repo.store._rows("SELECT COUNT(*) FROM observations") == [(2,)]


def test_completed_batch_clock_cannot_be_reused_by_another_source(graph):
    repo, (a, b, *_) = graph
    receipt = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    with pytest.raises(ValidationError):
        ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20, provider=provider())
    with pytest.raises(ValidationError):
        repo.append_node(replace(b, known_from_ns=20, label="late node knowledge"))
    assert repo.batch(receipt.batch_id) == receipt
    assert repo.store._rows("SELECT COUNT(*) FROM graph_batches") == [(1,)]


@pytest.mark.parametrize("artifact", ["snapshot", "propagation"])
def test_another_connection_cannot_write_between_artifact_validation_and_sealing(
    graph, tmp_path, monkeypatch, artifact
):
    import eie.graph.validation as validation

    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    snapshot = traverse(repo, (a.node_id,), 30, 10, traversal())
    propagation = propagate(event(a.node_id), repo, 30, 10, traversal())
    with SQLiteEventStore(tmp_path / "graph.sqlite") as other:
        other._connection.execute("PRAGMA busy_timeout=0")
        writer = GraphRepository(other)
        attempts = []
        name = "validate_snapshot" if artifact == "snapshot" else "validate_propagation"
        original = getattr(validation, name)

        def guarded(value, reader=None):
            result = original(value, reader)
            if not attempts and reader is repo:
                attempts.append(True)
                with pytest.raises(PersistenceError):
                    ingest(writer, (relationship("late", R.PEER_OF, a, b),), 25, resource="late")
            return result

        monkeypatch.setattr(validation, name, guarded)
        if artifact == "snapshot":
            assert repo.append_snapshot(snapshot)
        else:
            assert repo.append_propagation(propagation)
        assert attempts == [True] and repo.snapshot(snapshot.snapshot_id) == snapshot
        with pytest.raises(ValidationError):
            ingest(writer, (relationship("late", R.PEER_OF, a, b),), 25, resource="late")
        assert repo.store._rows("SELECT COUNT(*) FROM graph_batches") == [(1,)]


def test_graph_dataset_requires_the_exact_frozen_historical_manifest(tmp_path):
    from historical_helpers import fixture, workspace

    from eie.backfill.models import JobStatus
    from eie.graph.nodes import GraphDataset
    from examples.entity_relationship_graph import fixture_nodes

    job, source, processor = fixture()
    with workspace(tmp_path, ((source, processor),)) as (datasets, _, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        manifest = datasets.manifest(job.build_id)
        dataset = GraphDataset(
            "frozen-relationships", manifest.content_fingerprint, "1", job.build_id
        )
        repo = GraphRepository(datasets.store)
        nodes = tuple(replace(n, dataset=dataset) for n in fixture_nodes())
        for n in nodes:
            repo.append_node(n)
        p = replace(provider(), dataset=dataset)
        receipt = ingest(
            repo, (relationship("s", R.SUPPLIER_TO, nodes[0], nodes[1]),), 20, provider=p
        )
        assert repo.batch(receipt.batch_id).provider.dataset == dataset
        wrong = replace(dataset, fingerprint="f" * 64)
        with pytest.raises(ValidationError):
            repo.append_node(replace(nodes[0], known_from_ns=21, dataset=wrong))
        with pytest.raises(ValidationError):
            ingest(
                repo,
                (relationship("s", R.PEER_OF, nodes[0], nodes[1]),),
                22,
                provider=replace(p, dataset=wrong),
            )
        assert datasets.store._rows("SELECT COUNT(*) FROM graph_batches") == [(1,)]


def test_root_evidence_and_missing_endpoint_fail_closed(graph, monkeypatch):
    repo, (a, b, c, *_) = graph
    roots = tuple(sorted((a.node_id, b.node_id, c.node_id)))
    initial = traverse(repo, roots, 20, 10, traversal(max_depth=0))
    budget = len(codec.dumps(initial)) + 256
    with pytest.raises(ValidationError, match="root evidence"):
        traverse(repo, roots, 20, 10, traversal(max_depth=0, max_result_bytes=budget))
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    original = repo.node

    def missing(identity, known):
        return None if identity == b.node_id else original(identity, known)

    # Resolve valid retained adjacency before emulating an incomplete external reader.
    edges = GraphQueries(repo).customers(a.node_id, 20, 10)
    monkeypatch.setattr(GraphQueries, "neighbors", lambda *args, **kwargs: edges)
    monkeypatch.setattr(repo, "node", missing)
    with pytest.raises(ValidationError, match="endpoint missing"):
        traverse(repo, (a.node_id,), 20, 10, traversal())


def test_voting_power_relationship_does_not_become_derived_equity_ownership(graph):
    repo, (a, b, *_) = graph
    vote = ExposureMeasurement(
        ExposureType.VOTING_POWER, 10, "audited voting disclosure", point=0.8
    )
    ingest(repo, (relationship("parent", R.PARENT_OF, a, b, exposure=vote),), 20)
    result = traverse(
        repo,
        (a.node_id,),
        20,
        10,
        traversal(R.PARENT_OF, derive_ownership=True, direction=Direction.OUTGOING),
    )
    assert result.paths[0].derived_exposure is None
    assert {q.value for q in result.paths[0].quality} >= {"EXPOSURE_UNAVAILABLE"}


def test_correction_can_change_endpoint_without_rewriting_earlier_claim(graph):
    repo, (a, b, c, *_) = graph
    first = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    old = repo.claim_revision(first.claim_revision_ids[0])
    before = traverse(repo, (a.node_id,), 30, 10, traversal())
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, c),), 40)
    after = traverse(repo, (a.node_id,), 40, 10, traversal())
    assert before == traverse(repo, (a.node_id,), 30, 10, traversal())
    assert (
        before.edges[0].target_node_id == b.node_id and after.edges[0].target_node_id == c.node_id
    )
    history = repo.claim_history(old.claim_id)
    assert len(history) == 2 and history[0].edge_id != history[1].edge_id
    assert validate_claim_history(history, repo).valid
    diff = snapshot_diff(before, after)
    assert diff.added == (history[1].edge_id,) and diff.removed == (old.edge_id,)


def test_verified_cik_and_stable_primary_alias_cannot_be_rebound(graph):
    repo, (a, _, _, _, security, _, _, index, *_) = graph
    replacement = replace(a.foundation, cik="0000000042")
    with pytest.raises(ValidationError):
        repo.append_node(foundation_node(replacement, SOURCE, DATASET, 10))
    with pytest.raises(ValidationError):
        replace(a, identifiers=(NodeIdentifier("SEC_CIK", "0000000042"), *a.identifiers))
    aliases = tuple(
        sorted(
            (NodeIdentifier("AAA", "alias"), *index.identifiers),
            key=lambda i: (i.authority, i.value),
        )
    )
    revised = replace(
        index, known_from_ns=10, identifiers=aliases, identity_identifier=index.identifiers[0]
    )
    assert repo.append_node(revised) and revised.node_id == index.node_id
    with pytest.raises(ValidationError):
        replace(index, known_from_ns=10, identifiers=aliases)
    with pytest.raises(ValidationError):
        repo.append_node(replace(a, dataset=replace(DATASET, build_id="unknown"), known_from_ns=11))
    orphan = replace(security.foundation, issuer_id="iss_" + "f" * 64)
    with pytest.raises(ValidationError):
        repo.append_node(foundation_node(orphan, SOURCE, DATASET, 11))
    issuer_revision = SecIssuerRevision(
        a.node_id, CIK(a.foundation.cik), "Audited issuer", (), 10, "obs_" + "0" * 64
    )
    with pytest.raises(ValidationError):
        foundation_node(issuer_revision, SOURCE, DATASET, 9)


def test_exact_symbol_requires_agreement_with_optional_registry(graph):
    repo, nodes = graph
    listings(repo, nodes)
    endpoint = Endpoint(IdentifierKind.SYMBOL, "REUSE", GraphNodeType.ISSUER, exchange="XNAS")
    assert GraphIngestor(repo, registry=SecEntityRegistry()).resolve(endpoint, 20, 10) is None
    e = event(nodes[0].node_id)
    from eie.events.scope import EventScope, ScopeKind

    e = replace(e, identity=replace(e.identity, scope=EventScope(ScopeKind.SYMBOL, ("REUSE",))))
    with pytest.raises(ValidationError):
        propagate(e, repo, 30, 10, traversal(), exchange="XNAS", registry=SecEntityRegistry())
    with pytest.raises(ValidationError):
        propagate(e, repo, 30, 10, traversal(), exchange="XNYS")


def test_exact_zero_and_underflow_are_distinct_in_ownership_ranges():
    zero = ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 0, "audited", point=0)
    bounds = ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 0, "audited", lower=0.1, upper=0.2)
    assert compose_ownership((zero, bounds), ("a", "b")).point == 0
    assert compose_ownership((zero, None), ("a", "b")) is None
    tiny = replace(bounds, lower=1e-250)
    with pytest.raises(ValidationError, match="lower-bound underflow"):
        compose_ownership((tiny, tiny), ("a", "b"))


def test_exhaustive_nested_etf_snapshot_only_closes_its_own_holdings(graph):
    repo, nodes = graph
    security, etf = nodes[4], nodes[6]
    other = replace(
        etf,
        node_id=node_id(GraphNodeType.ETF, "SYNTHETIC", "other-fund"),
        identifiers=(NodeIdentifier("SYNTHETIC", "other-fund"),),
        label="other fund",
    )
    repo.append_node(other)
    p = provider(snapshot_policy=SnapshotPolicy.CARRY_UNTIL_NEXT, allow_implied_removal=True)
    nested = relationship("nested", R.ETF_HOLDS, other, etf)
    ingest(repo, (nested,), 19, provider=p, resource="holdings")
    ingest(
        repo,
        (relationship("holding", R.ETF_HOLDS, etf, security),),
        20,
        provider=p,
        resource="holdings",
        snapshot=SnapshotScope(endpoint(etf), R.ETF_HOLDS, 10, True, True),
    )
    empty = ingest(
        repo,
        (),
        30,
        provider=p,
        resource="holdings",
        snapshot=SnapshotScope(endpoint(etf), R.ETF_HOLDS, 20, True, True),
    )
    assert len(empty.claim_revision_ids) == 1
    assert validate_ingestion(empty, repo).valid
    remaining = GraphQueries(repo).etf_exposure(etf.node_id, 30, 25)
    assert len(remaining) == 1 and remaining[0].source_node_id == other.node_id


@pytest.mark.parametrize("change", ["number", "schema", "evidence", "predecessor", "reference"])
def test_claim_contract_refuses_invalid_history_evidence_and_measurement_clock(graph, change):
    repo, (a, b, *_) = graph
    receipt = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    claim = repo.claim_revision(receipt.claim_revision_ids[0])
    changes = {
        "number": {"number": 0},
        "schema": {"schema_version": "2.0"},
        "evidence": {"observation_id": "arbitrary"},
        "predecessor": {"number": 2},
        "reference": {
            "item": replace(
                claim.item,
                exposure=ExposureMeasurement(ExposureType.REVENUE_SHARE, 21, "audited", point=0.1),
            )
        },
    }
    with pytest.raises(ValidationError):
        replace(claim, **changes[change])


@pytest.mark.parametrize("change", ["self", "empty", "inverse", "confirmation", "identity"])
def test_canonical_edge_contract_refuses_invalid_semantic_identity(graph, change):
    repo, (a, b, c, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    edge = GraphQueries(repo).customers(a.node_id, 20, 10)[0]
    changes = {
        "self": {"target_node_id": a.node_id},
        "empty": {"claims": ()},
        "inverse": {
            "relationship_type": R.CUSTOMER_OF,
            "source_node_id": b.node_id,
            "target_node_id": a.node_id,
        },
        "confirmation": {"confirmation_count": 0},
        "identity": {"target_node_id": c.node_id},
    }
    with pytest.raises(ValidationError):
        replace(edge, **changes[change])


@pytest.mark.parametrize("change", ["schema", "roots", "nodes", "paths", "bounds"])
def test_snapshot_contract_refuses_invalid_order_identity_and_bounds(graph, change):
    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    snapshot = traverse(repo, (a.node_id,), 20, 10, traversal())
    changes = {
        "schema": {"schema_version": "2.0"},
        "roots": {"roots": (a.node_id, a.node_id)},
        "nodes": {"nodes": tuple(reversed(snapshot.nodes))},
        "paths": {"paths": snapshot.paths * 2},
        "bounds": {"traversal": traversal(max_nodes=1)},
    }
    with pytest.raises(ValidationError):
        replace(snapshot, **changes[change])
    with pytest.raises(ValidationError):
        RelationshipPath((a.node_id, "wrong"), snapshot.edges)


def test_claim_history_validation_independent_of_persistence(graph):
    repo, (a, b, *_) = graph
    receipt = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    claim = repo.claim_revision(receipt.claim_revision_ids[0])
    assert validate_claim_history((claim,), repo).valid
    wrong_chain = replace(claim, number=2, previous_revision_id=claim.revision_id)
    assert "INVALID_CLAIM_REVISION_CHAIN" in validate_claim_history((wrong_chain,), repo).reasons
    wrong_endpoint = replace(claim, source_node_id=b.node_id, target_node_id=a.node_id)
    assert (
        "INVALID_CLAIM_ENDPOINT_OR_EXPOSURE"
        in validate_claim_history((wrong_endpoint,), repo).reasons
    )
    wrong_exposure = replace(
        claim,
        item=replace(
            claim.item,
            exposure=ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 10, "audited", point=0.1),
        ),
    )
    assert not validate_claim_history((wrong_exposure,)).valid


@pytest.mark.parametrize("change", ["raw", "missing_claim", "canonical", "unresolved", "conflict"])
def test_ingestion_receipt_recalculation_refuses_forged_evidence_and_counts(graph, change):
    repo, (a, b, *_) = graph
    receipt = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    changes = {
        "raw": {"observation_id": "obs_" + "f" * 64},
        "missing_claim": {"claim_revision_ids": ("unknown",)},
        "canonical": {"canonical_revision_ids": ()},
        "unresolved": {"unresolved_count": 1},
        "conflict": {"conflict_count": 1},
    }
    assert not validate_ingestion(replace(receipt, **changes[change]), repo).valid


@pytest.mark.parametrize(
    "change,reason",
    [
        ("roots", "MISSING_ROOT"),
        ("nodes", "MISSING_ENDPOINT"),
        ("future_node", "FUTURE_NODE"),
        ("time", "INVALID_EDGE_TIME"),
        ("filter", "PATH_RELATIONSHIP_FILTER"),
        ("direction", "PATH_DIRECTION_MISMATCH"),
        ("exposure", "PATH_EXPOSURE_MISMATCH"),
        ("resolution", "EDGE_RESOLUTION_MISMATCH"),
        ("incomplete", "HISTORICAL_RECALCULATION_MISMATCH"),
    ],
)
def test_snapshot_pure_and_complete_recalculation_refuse_tampering(graph, change, reason):
    repo, (a, b, c, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    original = traverse(repo, (a.node_id,), 30, 10, traversal(R.SUPPLIER_TO))
    changes = {
        "roots": {"roots": (c.node_id,)},
        "nodes": {"nodes": (a,)},
        "future_node": {"nodes": tuple(replace(n, known_from_ns=40) for n in original.nodes)},
        "time": {"valid_at_ns": 5},
        "filter": {"traversal": traversal(R.PEER_OF)},
        "direction": {"traversal": traversal(R.SUPPLIER_TO, direction=Direction.INCOMING)},
        "exposure": {
            "paths": (
                replace(
                    original.paths[0],
                    derived_exposure=DerivedPathExposure(
                        ExposureType.OWNERSHIP_PERCENT, 0.1, None, None, True, True, ("fabricated",)
                    ),
                ),
            )
        },
        "resolution": {"edges": (replace(original.edges[0], confirmation_count=2),)},
        "incomplete": {"paths": ()},
    }
    modified = replace(original, **changes[change])
    assert reason in validate_snapshot(modified, repo).reasons
    with pytest.raises(ValidationError):
        repo.append_snapshot(modified)


def test_missing_historical_inputs_are_reported(graph, monkeypatch):
    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    snapshot = traverse(repo, (a.node_id,), 30, 10, traversal())

    def failed(*args, **kwargs):
        raise ValidationError("retained input unavailable")

    monkeypatch.setattr(repo, "current_claims", failed)
    assert "HISTORICAL_INPUT_INVALID" in validate_snapshot(snapshot, repo).reasons


@pytest.mark.parametrize("change", ["scope", "binding", "method", "cutoff", "unexpanded", "reason"])
def test_propagation_revalidates_scope_cutoff_and_candidate_paths(graph, change):
    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    result = propagate(event(a.node_id), repo, 30, 10, traversal())
    binding = result.bindings[0]
    changes = {
        "scope": {"bindings": (replace(binding, scope_identifier="unknown"),)},
        "binding": {"bindings": ()},
        "method": {"bindings": (replace(binding, method="EXACT_HISTORICAL_SYMBOL"),)},
        "cutoff": {"valid_at_ns": 11},
        "unexpanded": {"snapshot": None},
        "reason": {"reasons": ("fabricated",)},
    }
    forged = replace(result, **changes[change])
    assert not validate_propagation(forged, repo).valid
    with pytest.raises(ValidationError):
        repo.append_propagation(forged)


def test_candidates_require_unique_related_paths_and_available_event(graph):
    repo, (a, b, *_) = graph
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    result = propagate(event(a.node_id), repo, 30, 10, traversal())
    candidate = result.candidates[0]
    for changes in ({"paths": ()}, {"candidate_entity": a.node_id}, {"paths": candidate.paths * 2}):
        with pytest.raises(ValidationError):
            replace(candidate, **changes)
    with pytest.raises(ValidationError):
        replace(result, candidates=(candidate, candidate))
    with pytest.raises(ValidationError):
        replace(result, known_as_of_ns=24)
    with pytest.raises(ValidationError):
        propagate(event("iss_" + "f" * 64), repo, 30, 10, traversal())
    with pytest.raises(ValidationError):
        traverse(repo, ("unknown",), 30, 10, traversal())


def test_independent_raw_rebuild_and_hash_seed_determinism(tmp_path):
    fixture = codec.loads(Path("tests/fixtures/relationships/graph_history.json").read_bytes())
    nodes, provider, batches = fixture
    results = []
    for name in ("first", "second"):
        with SQLiteEventStore(tmp_path / f"{name}.sqlite") as store:
            repo = GraphRepository(store)
            for n in nodes:
                repo.append_node(n)
            engine = GraphIngestor(repo)
            receipts = tuple(
                engine.ingest(RelationshipAdapter(provider), codec.dumps(b)) for b in batches
            )
            snapshot = traverse(repo, (nodes[0].node_id,), 30, 25, traversal(derive_ownership=True))
            result = propagate(
                event(nodes[0].node_id), repo, 30, 25, traversal(derive_ownership=True)
            )
            results.append(codec.dumps((receipts, snapshot, result)))
    assert results[0] == results[1]
    script = (
        "import socket, runpy; "
        "socket.socket.connect=lambda *a, **k: "
        "(_ for _ in ()).throw(AssertionError('offline')); "
        "socket.socket.connect_ex=socket.socket.connect; "
        "socket.create_connection=socket.socket.connect; "
        "runpy.run_path('examples/entity_relationship_graph.py', run_name='__main__')"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
        )
        for seed in ("1", "8675309")
    ]
    assert json.loads(outputs[0]) == json.loads(outputs[1])
