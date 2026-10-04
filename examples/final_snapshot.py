"""Offline final snapshot spanning all retained EIE layers; synthetic market scenario."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.analogues.engine import AnalogueEngine
from eie.analogues.models import AnalogueQuery
from eie.config.settings import Settings
from eie.core.serialization import canonical_json_bytes
from eie.corporate.models import CorporateKind
from eie.corporate.settings import CorporateSettings
from eie.entities.models import Issuer, Security, security_id
from eie.entities.sec import (
    SecEntityRegistry,
    SecIssuerRevision,
    SecListingRevision,
    sec_listing_id,
)
from eie.events.scope import EventScope, ScopeKind
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository
from eie.expectations.models import EPS_DILUTED
from eie.graph.models import TraversalPolicy
from eie.graph.nodes import foundation_node
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType
from eie.graph.traversal import traverse
from eie.impact.engine import ImpactEngine
from eie.impact.inputs import ImpactInputs
from eie.impact.models import ModelType
from eie.impact.repository import ImpactRepository
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import InteractionPolicy, InteractionQuery
from eie.interactions.repository import InteractionRepository
from eie.nlp.engine import TextEngine
from eie.nlp.repository import TextRepository
from eie.persistence.codec import loads
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.ingestion import OfficialSourceIngestor
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.corporate.adapter import CorporateAdapter, CorporateIngestor
from eie.providers.sec.adapter import SecIngestor
from eie.providers.sec.models import CIK
from eie.providers.sec.settings import SecSettings
from eie.reliability.models import (
    CoverageSlot,
    CoverageUniverse,
    ReliabilityQuery,
    SourceReliabilityPolicy,
)
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.scheduled.index import ScheduledEventIndex
from eie.service.engine import EventIntelligenceService
from eie.service.evaluation import evaluate
from eie.service.models import EIESnapshot, RetainedExpectation, SnapshotRequest
from eie.service.repository import ServiceRepository
from eie.service.schema import OwnerKind as K
from eie.service.schema import ValidationDepth
from eie.service.validation import validate
from eie.sources.trust import SourceClass
from examples.analogue_fixtures import fixture
from examples.counterfactual_impact import CARS, ESTIMATION, supplied_market
from examples.entity_relationship_graph import DATASET as GRAPH_DATASET
from examples.entity_relationship_graph import SOURCE, ingest, relationship
from examples.event_studies import DAY, history
from examples.expectations_replay import build_example
from examples.intelligence_fixtures import ISSUER
from examples.reliability_fixtures import DATASET as RELIABILITY_DATASET
from examples.reliability_fixtures import SECOND
from examples.reliability_fixtures import ingest as ingest_claim
from examples.text_demo import CATALOG as TEXT_CATALOG
from examples.text_demo import DATASET as TEXT_DATASET
from examples.text_demo import item, source

# September 1, 2026 UTC; source and processing availability stay separate.
ORIGIN = 1_788_220_800_000_000_000
CUTOFF = ORIGIN + 40 * DAY
VALID = ORIGIN + 9 * DAY + 600 * SECOND
CATALOG = "final-offline"
FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures"
SID = security_id("SYNTHETIC_REFERENCE", "example-common")


@dataclass
class Scenario:
    store: SQLiteEventStore
    service: EventIntelligenceService
    snapshot: EIESnapshot
    graph_snapshot_id: str
    text_run_id: str


def build(root: Path) -> Scenario:
    root.mkdir(parents=True, exist_ok=True)
    archived = history(root, (3, 5, 7, 9), issuer=ISSUER, clock_offset=ORIGIN)
    data = fixture(root, historical=archived, market_factory=supplied_market)
    store = data.store
    repo = ServiceRepository(store)
    graph = GraphRepository(store)
    foundations = (
        Issuer(ISSUER, "Example Corporation", "0000320193"),
        Security(SID, ISSUER, "Common shares"),
        SecListingRevision(
            sec_listing_id(SID, "EXM", "NYSE"),
            ISSUER,
            SID,
            "EXM",
            "NYSE",
            ORIGIN,
            None,
            ORIGIN,
            "obs_synthetic_listing",
        ),
    )
    nodes = tuple(foundation_node(f, SOURCE, GRAPH_DATASET, ORIGIN) for f in foundations)
    other = foundation_node(
        Issuer("iss_" + "f" * 64, "Synthetic supplier"), SOURCE, GRAPH_DATASET, ORIGIN
    )
    for node in (*nodes, other):
        graph.append_node(node)
    ingest(
        graph,
        (relationship("supplier", RelationshipType.SUPPLIER_TO, nodes[0], other, start=ORIGIN),),
        ORIGIN + DAY,
    )
    context = traverse(
        graph, (ISSUER,), VALID, VALID, TraversalPolicy((RelationshipType.SUPPLIER_TO,))
    )
    graph.append_snapshot(context)

    official = OfficialSourceIngestor(
        ScheduledEventIndex(Settings(official=OfficialProviderSettings(enabled_ids=("BLS",))))
    )
    official.ingest_payload(
        BLSAdapter(), (FIXTURES / "official/bls_schedule.json").read_bytes(), ORIGIN, ORIGIN
    )
    store.save_session("final-official", official)
    sec = SecIngestor(Settings(sec=SecSettings(enabled=True, allow_issuer_bootstrap=True)))
    sec.ingest_payload(
        (FIXTURES / "sec/submissions.json").read_bytes(),
        "https://data.sec.gov/submissions/CIK0000123456.json",
        ORIGIN,
        ORIGIN,
    )
    store.save_session("final-sec", sec)
    registry = SecEntityRegistry()
    registry.add_issuer(
        SecIssuerRevision(
            "iss_e913f2e0cdc1f7df12cc61b1d1ae670d47005771ef5afbf21e828d65e95cc25c",
            CIK("0000123456"),
            "Alpha Holdings",
            (),
            0,
            "obs_synthetic_issuer",
        )
    )
    corporate = CorporateIngestor(Settings(corporate=CorporateSettings(enabled=True)), registry)
    corporate.ingest_payload(
        CorporateAdapter("SYNTH_A", SourceClass.COMPANY_PRIMARY, frozenset(CorporateKind)),
        (FIXTURES / "corporate/provider_a.json").read_bytes(),
        ORIGIN + DAY,
        ORIGIN + DAY,
    )
    store.save_session("final-corporate", corporate)
    estimates, estimate_event = build_example()
    store.save_session("final-estimates", estimates)
    surprise = estimates.engine.surprise(estimate_event, EPS_DILUTED, 300, provider_id="SYNTH_A")
    repo.retain_expectation(
        CATALOG, RetainedExpectation("final-estimates", "SYNTH_A", surprise), CUTOFF
    )

    p = source("FINAL_OFFICIAL")
    doc, body = item(p, observed=ORIGIN + 21 * DAY)
    run = TextEngine(TextRepository(store)).ingest(
        p, doc, body, TEXT_CATALOG, ORIGIN + 21 * DAY + SECOND
    )
    evidence = EvidenceRepository(store)
    queries = EvidenceQueries(evidence, TEXT_DATASET.dataset_id)
    with store.transaction():
        for claim in queries.claims(ORIGIN + 22 * DAY):
            resolved = queries.resolution(claim.claim_id, ORIGIN + 22 * DAY)
            inputs = evidence.observations(
                TEXT_DATASET.dataset_id,
                resolved.known_as_of_ns,
                claim_id=claim.claim_id,
                limit=10000,
            )
            links = evidence.relationships(
                TEXT_DATASET.dataset_id,
                resolved.known_as_of_ns,
                tuple(r.observation_id for r in inputs),
                10000,
            )
            evidence.append_resolution(resolved, inputs, links)
    queries.resolution_as_of(ORIGIN + 22 * DAY)
    target = data.rows[-1]
    study = data.studies[-1]
    analogue = AnalogueEngine(data.repository).analogues(
        AnalogueQuery(
            target.row_id,
            target.event_id,
            target.revision,
            target.dataset_id,
            target.feature_cutoff_ns,
            target.available_ns,
            data.rows[0].policy_id,
            data.spec,
        )
    )
    response = data.repository.index_response(target.row_id, study.study_id, data.spec)
    data.repository.attach_reaction(analogue.result_id, response.response_id, CUTOFF - SECOND)
    benchmark = supplied_market(
        study.definition.anchor.timestamp_ns, "market_fixture", target=False
    )
    impact = ImpactEngine().impact_event(
        ImpactInputs(study, study.market, benchmark), ModelType.MARKET_ADJUSTED, ESTIMATION, CARS
    )
    ImpactRepository(store).append(impact)
    interaction = InteractionRepository(store)
    for event_id in archived[2]:
        interaction.index_event("analogue-history", event_id, ORIGIN + 24 * DAY)
    policy = InteractionPolicy()
    interaction.append_policy(policy)
    InteractionEngine(interaction).interaction_context(
        InteractionQuery(
            "analogue-history",
            target.event_id,
            ORIGIN + 24 * DAY,
            policy.artifact_id,
            context.snapshot_id,
        )
    )

    _, row = ingest_claim(
        store, "FINAL_PRIMARY", ORIGIN + 23 * DAY, publication=ORIGIN + 23 * DAY - 3 * SECOND
    )
    reliability = ReliabilityQueries(ReliabilityRepository(store))
    universe = CoverageUniverse(
        RELIABILITY_DATASET.dataset_id,
        "FINAL_PRIMARY",
        "financial",
        ISSUER,
        ORIGIN,
        CUTOFF,
        ORIGIN + 22 * DAY,
        "synthetic-release-calendar",
        "a" * 64,
        (CoverageSlot("release", ORIGIN + 23 * DAY, "EARNINGS.RELEASE", ISSUER),),
    )
    reliability.repository.append_universe(universe)
    reliability.source_reliability(
        ReliabilityQuery(
            RELIABILITY_DATASET.dataset_id,
            "FINAL_PRIMARY",
            "financial",
            CUTOFF - SECOND,
            SourceReliabilityPolicy(minimum_count=1),
            scope_id=ISSUER,
            coverage_universe_id=universe.artifact_id,
        )
    )
    reliability.latency(row.artifact_id, CUTOFF - SECOND)
    reliability.coverage(universe.artifact_id, CUTOFF - SECOND)

    # Fixed native owner tables and IDs, never dynamic imports or copied raw text.
    for identity, revision in store._rows("SELECT id,revision FROM events ORDER BY id,revision"):
        repo.publish(CATALOG, K.EVENT, (str(identity), str(revision)), CUTOFF)
    for identity, raw in store._rows("SELECT id,payload FROM domain_records ORDER BY id"):
        value = loads(raw)
        if type(value).__name__ in (
            "ScheduleRevision",
            "CorporateEvent",
            "FilingMetadata",
            "ActualObservation",
            "EstimateObservation",
            "GuidanceObservation",
        ):
            repo.publish(CATALOG, K.DOMAIN, (str(identity),), CUTOFF)
    tables = (
        (K.NODE, "graph_nodes"),
        (K.GRAPH, "graph_snapshots"),
        (K.EXTRACTION, "te_runs"),
        (K.RESOLUTION, "ce_claim_resolutions"),
        (K.EVENT_RESOLUTION, "ce_event_runs"),
        (K.INTELLIGENCE, "ei_results"),
        (K.STUDY, "study_results"),
        (K.IMPACT, "impact_results"),
        (K.ANALOGUE, "analogue_results"),
        (K.REACTION, "analogue_reactions"),
        (K.INTERACTION, "interaction_contexts"),
    )
    for kind, table in tables:
        for (identity,) in store._rows("SELECT id FROM " + table + " ORDER BY id"):
            repo.publish(CATALOG, kind, (str(identity),), CUTOFF)
    for identity, kind in store._rows("SELECT id,kind FROM reliability_artifacts ORDER BY id"):
        mapping = {
            "SourceReliabilityResult": K.RELIABILITY,
            "InformationLatency": K.LATENCY,
            "SourceCoverageResult": K.COVERAGE,
        }
        if kind in mapping:
            repo.publish(CATALOG, mapping[kind], (str(identity),), CUTOFF)
    repo.publish(CATALOG, K.DATASET, (archived[1].build_id,), CUTOFF)
    service = EventIntelligenceService(repo)
    snapshot = service.snapshot(
        SnapshotRequest(CATALOG, EventScope(ScopeKind.MARKET), CUTOFF, VALID)
    )
    assert validate(repo, snapshot.snapshot_id, ValidationDepth.DEEP).valid
    return Scenario(store, service, snapshot, context.snapshot_id, run.run_id)


def run(root: Path) -> dict[str, object]:
    scenario = build(root)
    original = canonical_json_bytes(scenario.snapshot)
    summary = {
        c.kind.value: {
            "state": c.state.value,
            "references": len(c.references),
            "completeness": c.completeness.value,
        }
        for c in scenario.snapshot.components
    }
    evaluation = evaluate(scenario.service.repository, scenario.snapshot.snapshot_id)
    request = scenario.snapshot.request
    scenario.store.close()
    with SQLiteEventStore(root / "studies.sqlite") as store:
        provider = EventIntelligenceService(ServiceRepository(store))
        assert canonical_json_bytes(provider.snapshot(request)) == original
    return {
        "snapshot_id": scenario.snapshot.snapshot_id,
        "bytes": len(original),
        "components": summary,
        "evaluation": evaluation,
        "restart_identical": True,
    }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(canonical_json_bytes(run(Path(directory))).decode())
