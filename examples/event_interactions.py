"""Offline bundle/macro collision, subsequent related event, and independent materiality."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.serialization import canonical_json_bytes
from eie.corporate.actions import Buyback
from eie.corporate.types import Money
from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import ActionValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.references import EvidenceLocator, LocatorKind
from eie.evidence.repository import EvidenceRepository
from eie.expectations.units import Quantity
from eie.graph.models import TraversalPolicy
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType
from eie.graph.traversal import traverse
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import (
    InteractionPolicy,
    InteractionQuery,
    InteractionWindow,
    WindowOverride,
)
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import OrderingBasis
from eie.interactions.validation import validate
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.normalization import field_reference
from examples.entity_relationship_graph import fixture_nodes, ingest, relationship
from examples.intelligence_fixtures import DATASET, USD, batch, source
from examples.interaction_fixtures import MINUTE, retained_event


def bundle(provider, issuer):
    first = batch(
        provider, 10 * MINUTE, Quantity(2_000_000_000, USD), key="earnings", subject_id=issuer
    )
    second = batch(
        provider,
        10 * MINUTE,
        ActionValue(Buyback(Money(100_000_000_000_000, "USD"))),
        key="buyback",
        subject_id=issuer,
        event_type=EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.BUYBACK"),
    )
    raw = replace(
        first.raw,
        source_item_id="earnings-buyback-bundle",
        payload_hash=sha256(
            canonical_json_bytes((first.claims[0].value, second.claims[0].value))
        ).hexdigest(),
    )
    refs = tuple(
        field_reference(
            raw.observation_id,
            raw.source_item_id,
            raw.payload_hash,
            provider.parser_version,
            provider.source.adapter_version,
            EvidenceLocator(LocatorKind.JSON_POINTER, "/" + label),
            old.claims[0].value,
        )
        for label, old in (("earnings", first), ("buyback", second))
    )
    events = tuple(
        replace(old.events[0], evidence_observation_ids=(raw.observation_id,))
        for old in (first, second)
    )
    claims = tuple(
        replace(old.claims[0], evidence_ids=(ref.evidence_id,))
        for old, ref in zip((first, second), refs, strict=True)
    )
    return replace(
        first,
        raw=raw,
        lineage=replace(first.lineage, raw_observation_id=raw.observation_id),
        references=tuple(sorted(refs, key=lambda r: r.evidence_id)),
        claims=tuple(sorted(claims, key=lambda c: c.provider_claim_key)),
        events=tuple(sorted(events, key=lambda e: e.revision_id)),
    )


def demo(root: Path) -> dict[str, object]:
    path = root / "interactions.sqlite"
    with SQLiteEventStore(path) as store:
        repo = InteractionRepository(store)
        graph = GraphRepository(store)
        nodes = fixture_nodes()
        a, b, *_ = nodes
        for node in nodes:
            graph.append_node(node)
        ingest(
            graph, (relationship("supplier-customer", RelationshipType.SUPPLIER_TO, a, b),), MINUTE
        )
        provider = source("BUNDLE_PRIMARY")
        combined = bundle(provider, a.node_id)
        EvidenceEngine(EvidenceRepository(store)).ingest(provider, combined)
        earnings = next(
            e for e in combined.events if e.identity.event_type.family is EventFamily.EARNINGS
        )
        buyback = next(e for e in combined.events if e is not earnings)
        for event in combined.events:
            repo.index_event(DATASET.dataset_id, event.event_id, 10 * MINUTE)
        retained_event(
            repo,
            "cpi",
            2 * MINUTE,
            scheduled=15 * MINUTE,
            scope=ScopeKind.MACRO_GLOBAL,
            family=EventFamily.MACRO,
            code="MACRO.CPI.US",
            dataset=DATASET.dataset_id,
        )
        graph_snapshot = traverse(
            graph,
            (a.node_id,),
            10 * MINUTE,
            10 * MINUTE,
            TraversalPolicy((RelationshipType.SUPPLIER_TO,)),
        )
        graph.append_snapshot(graph_snapshot)
        policy = InteractionPolicy(
            window=InteractionWindow(
                overrides=(
                    WindowOverride("MACRO", 10 * MINUTE, 10 * MINUTE, OrderingBasis.SCHEDULED_TIME),
                )
            )
        )
        repo.append_policy(policy)
        q = InteractionQuery(
            DATASET.dataset_id,
            earnings.event_id,
            10 * MINUTE,
            policy.artifact_id,
            graph_snapshot.snapshot_id,
        )
        initial = InteractionEngine(repo).interaction_context(q)
        before = dumps(initial)
        intel_repo = IntelligenceRepository(store)
        intel_policy = IntelligencePolicy()
        intel_repo.append_policy(intel_policy)
        intelligence = tuple(
            IntelligenceEngine(intel_repo).compute(
                IntelligenceRequest(
                    event.event_id,
                    1,
                    DATASET.dataset_id,
                    10 * MINUTE,
                    10 * MINUTE,
                    intel_policy.policy_id,
                )
            )
            for event in (earnings, buyback)
        )
        later = batch(provider, 30 * MINUTE, key="customer-event", subject_id=b.node_id)
        EvidenceEngine(EvidenceRepository(store)).ingest(provider, later)
        repo.index_event(DATASET.dataset_id, later.events[0].event_id, 30 * MINUTE)
        extended = InteractionEngine(repo).interaction_context(
            replace(q, known_as_of_ns=30 * MINUTE)
        )
        validate(repo, initial.artifact_id, deep=True)
        validate(repo, extended.artifact_id, deep=True)
        after = dumps(extended)
        materiality = {
            v.manifest.request.event_id: v.materiality.category.value for v in intelligence
        }
        output = {
            "initial_collisions": len(initial.collision_ids),
            "initial_categories": [k.value for k in initial.contamination],
            "later_related_events": len(extended.chronology_ids),
            "materiality_by_event": materiality,
            "interpretation": (
                "subsequent related event; no causal attribution or combined severity"
            ),
        }
    with SQLiteEventStore(path) as store:
        repo = InteractionRepository(store)
        assert dumps(repo.context(initial.artifact_id)) == before
        assert dumps(repo.context(extended.artifact_id)) == after
    output["historical_restart_bytes_identical"] = True
    return output


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), sort_keys=True, indent=2))
