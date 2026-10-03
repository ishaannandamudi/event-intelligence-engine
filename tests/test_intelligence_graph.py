"""Pinned graph knowledge, exposure ranges, aliases, and unresolved event identity."""

from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.evidence.schema import ClaimStatus
from eie.graph.exposure import ExposureMeasurement, ExposureType
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligenceLimits, IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.validation import validate
from eie.persistence.codec import dumps
from examples.intelligence_fixtures import batch, source
from tests.graph_helpers import graph, relationship, traversal
from tests.graph_helpers import ingest as graph_ingest
from tests.intelligence_helpers import compute, ingest, metrics, novelty, request, uncertainty

__all__ = ["graph"]


def test_eie11_later_merge_and_split_preserve_each_intelligence_snapshot():
    from eie.corporate.types import EarningsPeriod
    from eie.persistence.sqlite import SQLiteEventStore
    from tests.evidence_helpers import DATASET as ED
    from tests.evidence_helpers import item as event_item
    from tests.evidence_helpers import setup
    from tests.evidence_helpers import source as event_source

    with SQLiteEventStore(":memory:") as store:
        _, engine, query = setup(store)
        a, b = event_source("ALPHA"), event_source("BETA")
        first, second = event_item(a, 100), event_item(b, 200)
        weak = replace(
            second,
            descriptors=(replace(second.descriptors[0], period=None, corporate_identity=None),),
        )
        engine.ingest(a, first)
        engine.ingest(b, weak)
        repo = IntelligenceRepository(store)
        policy = IntelligencePolicy()
        repo.append_policy(policy)
        saved = []

        def snapshot(item, t):
            r = IntelligenceEngine(repo).compute(
                IntelligenceRequest(
                    item.events[0].event_id,
                    item.events[0].revision.number,
                    ED.dataset_id,
                    t,
                    t,
                    policy.policy_id,
                )
            )
            saved.append((r.intelligence_id, dumps(r)))
            return r

        old = snapshot(weak, 200)
        third = event_item(b, 300, prior=weak, status=ClaimStatus.UPDATED)
        engine.ingest(b, third)
        merged = snapshot(third, 300)
        fourth = event_item(b, 400, prior=third, status=ClaimStatus.CORRECTED)
        fourth = replace(
            fourth,
            descriptors=(
                replace(
                    fourth.descriptors[0],
                    period=EarningsPeriod(2026, 2, "2026-06-30"),
                    corporate_identity=None,
                ),
            ),
        )
        engine.ingest(b, fourth)
        split = snapshot(fourth, 400)
        assert len(old.manifest.excluded_event_ids) == 1
        assert len(merged.manifest.excluded_event_ids) == 2
        assert len(split.manifest.excluded_event_ids) == 1
        assert len(query.event_resolution(400).groups) == 2
        for identity, content in saved:
            assert dumps(repo.get(identity)) == content
            assert validate(repo, identity).state.value != "INVALID"


@pytest.mark.parametrize(
    "exposure",
    [
        None,
        ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 10, "synthetic", point=0.4),
        ExposureMeasurement(
            ExposureType.REVENUE_SHARE, 10, "synthetic", lower=0.1, upper=0.6, estimated=True
        ),
    ],
)
def test_graph_context_exposure_is_optional_and_never_zero(graph, exposure):
    repo, (a, b, *_) = graph
    store = repo.store
    graph_ingest(
        repo,
        (
            relationship(
                "ownership",
                R.SUPPLIER_TO
                if exposure is not None and exposure.measurement_type is ExposureType.REVENUE_SHARE
                else R.OWNS_EQUITY_IN,
                a,
                b,
                exposure=exposure,
            ),
        ),
        20,
    )
    snapshot = traverse(repo, (a.node_id,), 20, 10, traversal())
    repo.append_snapshot(snapshot)
    p = source()
    item = batch(p, 30, subject_id=a.node_id)
    ingest(store, p, item)
    result = compute(
        store, item, prior_graph_id=snapshot.snapshot_id, current_graph_id=snapshot.snapshot_id
    )
    assert novelty(result, "ENTITY")[0].state.value == "KNOWN_ENTITY"
    assert "KNOWN_IN_PRIOR_GRAPH" in novelty(result, "ENTITY")[0].reasons
    component = next(c for c in result.materiality.components if c.kind.value == "EXPOSURE")
    assert metrics(component)["point"] == (None if exposure is None else exposure.point)
    if exposure is None:
        assert component.category.value == "UNKNOWN"
    elif exposure.point is not None:
        assert component.category.value == "MAJOR"
    else:
        assert (
            "RANGE_CROSSES_CATEGORIES" in component.reasons
            and "ESTIMATED_EXPOSURE" in component.reasons
        )
    original = dumps(result)
    graph_ingest(repo, (relationship("new-source", R.SUPPLIER_TO, a, b),), 40)
    new = traverse(repo, (a.node_id,), 40, 10, traversal())
    repo.append_snapshot(new)
    assert (
        dumps(
            compute(
                store,
                item,
                prior_graph_id=snapshot.snapshot_id,
                current_graph_id=snapshot.snapshot_id,
            )
        )
        == original
    )
    assert validate(IntelligenceRepository(store), result.intelligence_id).state.value != "INVALID"
    with pytest.raises(ValidationError):
        compute(store, item, prior_graph_id=new.snapshot_id)
    with pytest.raises(ValidationError):
        compute(store, item, current_graph_id="missing")


def test_same_time_graph_is_context_but_never_prior_entity_evidence(graph):
    repo, (a, b, c, *_) = graph
    store = repo.store
    graph_ingest(
        repo, (relationship("one", R.PARENT_OF, a, b), relationship("two", R.SUPPLIER_TO, a, c)), 30
    )
    snapshot = traverse(repo, (a.node_id,), 30, 10, traversal())
    repo.append_snapshot(snapshot)
    p = source()
    item = batch(p, 30, subject_id=a.node_id)
    ingest(store, p, item)
    with pytest.raises(ValidationError):
        compute(store, item, prior_graph_id=snapshot.snapshot_id)
    result = compute(store, item, current_graph_id=snapshot.snapshot_id)
    assert novelty(result, "ENTITY")[0].state.value == "NEW_ENTITY"
    repo2 = IntelligenceRepository(store)
    with pytest.raises(ValidationError):
        IntelligenceEngine(repo2, limits=IntelligenceLimits(graph_edges=1)).compute(
            request(repo2, item, current_graph_id=snapshot.snapshot_id)
        )


def test_unrelated_graph_edges_do_not_become_event_exposure(graph):
    repo, (a, b, c, *_) = graph
    store = repo.store
    graph_ingest(
        repo,
        (
            relationship(
                "other",
                R.SUPPLIER_TO,
                b,
                c,
                exposure=ExposureMeasurement(ExposureType.REVENUE_SHARE, 10, "supplied", point=0.8),
            ),
        ),
        20,
    )
    snapshot = traverse(repo, (b.node_id,), 20, 10, traversal())
    repo.append_snapshot(snapshot)
    p = source()
    item = batch(p, 30, subject_id=a.node_id)
    ingest(store, p, item)
    result = compute(store, item, current_graph_id=snapshot.snapshot_id)
    c = next(c for c in result.materiality.components if c.kind.value == "EXPOSURE")
    assert c.category.value == "UNKNOWN" and c.reasons == ("EXPOSURE_NOT_SUPPLIED",)


def test_eie11_aliases_remain_responsible_for_identity_and_prior_source_confirmation():
    from eie.persistence.sqlite import SQLiteEventStore
    from tests.evidence_helpers import (
        DATASET as EVIDENCE_DATASET,
    )
    from tests.evidence_helpers import (
        item as evidence_item,
    )
    from tests.evidence_helpers import (
        setup,
    )
    from tests.evidence_helpers import (
        source as evidence_source,
    )

    with SQLiteEventStore(":memory:") as store:
        er, engine, q = setup(store)
        a, b = evidence_source("FIRST"), evidence_source("SECOND")
        first = evidence_item(a, 100)
        second = evidence_item(b, 200)
        engine.ingest(a, first)
        engine.ingest(b, second)
        repo = IntelligenceRepository(store)
        policy = IntelligencePolicy()
        repo.append_policy(policy)
        result = IntelligenceEngine(repo).compute(
            IntelligenceRequest(
                second.events[0].event_id,
                1,
                EVIDENCE_DATASET.dataset_id,
                200,
                200,
                policy.policy_id,
            )
        )
        assert len(result.manifest.excluded_event_ids) == 2
        assert uncertainty(result, "EVENT_IDENTITY").state.value == "LOW"
        assert uncertainty(result, "ANALOGUE").state.value == "READY_FOR_ANALOGUE_SEARCH"
        assert novelty(result, "CROSS_SOURCE")[0].state.value == "INDEPENDENT_CONFIRMATION"
        original = dumps(result)
        correction = evidence_item(
            b,
            300,
            replace(second.claims[0].value, timestamp_ns=second.claims[0].value.timestamp_ns + 1),
            prior=second,
            status=ClaimStatus.CORRECTED,
        )
        engine.ingest(b, correction)
        assert (
            dumps(repo.get(result.intelligence_id)) == original
            and validate(repo, result.intelligence_id).state.value != "INVALID"
        )
        with pytest.raises(ValidationError):
            IntelligenceEngine(repo).compute(
                replace(
                    result.manifest.request,
                    event_resolution_id=engine.repository.stored_event_run(
                        q.event_resolution_history(300)[-1].run_id
                    ).run_id,
                )
            )
