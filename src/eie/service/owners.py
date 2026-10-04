"""Static bridges to retained owners. Payloads remain in their original tables."""

from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
from typing import TypeAlias

from eie.analogues.models import AnalogueResult, MarketReactionSurprise
from eie.analogues.repository import AnalogueRepository
from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.corporate.models import CorporateEvent
from eie.datasets.models import DatasetManifest
from eie.datasets.repository import DatasetRepository
from eie.events.models import CanonicalEvent
from eie.events.scope import EventScope, ScopeKind
from eie.evidence.claims import TimePrecision, TimeValue
from eie.evidence.dedup import EventResolutionRun
from eie.evidence.models import CanonicalClaimResolution
from eie.evidence.repository import EvidenceRepository
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.observations import ActualObservation, EstimateObservation
from eie.expectations.surprise import SurpriseObservation
from eie.filings.models import FilingMetadata
from eie.graph.models import GraphSnapshot
from eie.graph.nodes import GraphNode
from eie.graph.propagation import PropagationResult
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import GraphNodeType
from eie.impact.models import EventImpactResult
from eie.impact.repository import ImpactRepository
from eie.intelligence.models import EventIntelligenceResult
from eie.intelligence.repository import IntelligenceRepository
from eie.interactions.models import InteractionContext
from eie.interactions.repository import InteractionRepository
from eie.nlp.models import ExtractionRun
from eie.nlp.repository import TextRepository
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore, _checked_event
from eie.providers.corporate.normalization import canonical_identity
from eie.reliability.clocks import exact
from eie.reliability.models import (
    CoverageUniverse,
    InformationClocks,
    InformationLatency,
    SourceCoverageResult,
    SourceReliabilityResult,
)
from eie.reliability.repository import ReliabilityRepository
from eie.scheduled.models import ScheduleRevision
from eie.service.models import (
    ArtifactReference,
    EventSummary,
    ProvenancePin,
    Publication,
    RetainedExpectation,
)
from eie.service.schema import ComponentKind as C
from eie.service.schema import ComponentState
from eie.service.schema import OwnerKind as K
from eie.studies.market import MarketSlice
from eie.studies.models import EventStudyResult
from eie.studies.repository import StudyRepository

Detail: TypeAlias = (
    CanonicalEvent
    | RetainedExpectation
    | GraphNode
    | ScheduleRevision
    | CorporateEvent
    | FilingMetadata
    | ActualObservation
    | EstimateObservation
    | GuidanceObservation
    | SurpriseObservation
    | CanonicalClaimResolution
    | EventResolutionRun
    | ExtractionRun
    | GraphSnapshot
    | PropagationResult
    | EventIntelligenceResult
    | EventStudyResult
    | EventImpactResult
    | AnalogueResult
    | MarketReactionSurprise
    | InteractionContext
    | SourceReliabilityResult
    | InformationLatency
    | SourceCoverageResult
    | DatasetManifest
)

# Fixed tables and columns; user data can only supply bound parameter values.
TABLES = {
    K.NODE: "graph_nodes",
    K.DOMAIN: "domain_records",
    K.SURPRISE: "ei_information",
    K.EXPECTATION: "service_expectations",
    K.RESOLUTION: "ce_claim_resolutions",
    K.EVENT_RESOLUTION: "ce_event_runs",
    K.EXTRACTION: "te_runs",
    K.GRAPH: "graph_snapshots",
    K.PROPAGATION: "graph_propagation",
    K.INTELLIGENCE: "ei_results",
    K.STUDY: "study_results",
    K.IMPACT: "impact_results",
    K.ANALOGUE: "analogue_results",
    K.REACTION: "analogue_reactions",
    K.INTERACTION: "interaction_contexts",
    K.RELIABILITY: "reliability_artifacts",
    K.LATENCY: "reliability_artifacts",
    K.COVERAGE: "reliability_artifacts",
    K.DATASET: "dataset_manifests",
}


def payload(store: SQLiteEventStore, kind: K, lookup: tuple[str, ...]) -> bytes:
    if kind is K.EVENT:
        if len(lookup) != 2 or not lookup[1].isdigit() or not 1 <= int(lookup[1]) <= 10000:
            raise ValidationError("exact event revision locator required")
        rows = store._rows(
            "SELECT payload FROM events WHERE id=? AND revision=?", (lookup[0], int(lookup[1]))
        )
    else:
        if len(lookup) != 1 or kind not in TABLES:
            raise ValidationError("exact retained owner locator required")
        key = "build_id" if kind is K.DATASET else "id"
        rows = store._rows("SELECT payload FROM " + TABLES[kind] + " WHERE " + key + "=?", lookup)
    if len(rows) != 1 or not isinstance(rows[0][0], bytes):
        raise PersistenceError("missing retained service owner")
    return rows[0][0]


def read(store: SQLiteEventStore, kind: K, lookup: tuple[str, ...]) -> Detail:
    raw = payload(store, kind, lookup)
    identity = lookup[0]
    value: object | None
    if kind is K.EVENT:
        rows = store._rows(
            "SELECT id,revision,available,payload FROM events WHERE id=? AND revision=?",
            (identity, int(lookup[1])),
        )
        value = _checked_event(rows[0])
    elif kind is K.NODE:
        node = codec.loads(raw)
        if not isinstance(node, GraphNode):
            raise PersistenceError("wrong graph node owner")
        value = GraphRepository(store).node(node.node_id, node.known_from_ns)
        if value is None or value.revision_id != identity:
            raise PersistenceError("graph node locator disagrees")
    elif kind is K.DOMAIN:
        value = codec.loads(raw)
        if (
            not isinstance(
                value,
                (
                    ScheduleRevision,
                    CorporateEvent,
                    FilingMetadata,
                    ActualObservation,
                    EstimateObservation,
                    GuidanceObservation,
                ),
            )
            or fingerprint(
                "eie:durable-contract:v1",
                (f"{type(value).__module__}.{type(value).__name__}", value),
            )
            != identity
        ):
            raise PersistenceError("unsupported or inconsistent domain owner")
    elif kind is K.EXPECTATION:
        candidate = codec.loads(raw)
        if not isinstance(candidate, RetainedExpectation) or candidate.artifact_id != identity:
            raise PersistenceError("retained expectation fingerprint mismatch")
        from eie.providers.estimates.adapter import EstimateIngestor

        session = store.load_session(candidate.namespace)
        if not isinstance(session, EstimateIngestor):
            raise PersistenceError("expectation source session missing")
        expected = session.engine.surprise(
            candidate.value.context.event_id,
            candidate.value.context.measurement_id,
            candidate.value.as_of_ns,
            provider_id=candidate.provider_id,
        )
        if expected != candidate.value:
            raise PersistenceError("retained expectation native query disagrees")
        value = candidate
    elif kind is K.SURPRISE:
        item = EvidenceRepository(store)._one("ei_information", identity, SurpriseObservation)
        if item is None or item[1] != (
            identity,
            "SURPRISE",
            item[0].context.event_id,
            item[0].as_of_ns,
            item[0].result_fingerprint,
        ):
            raise PersistenceError("surprise owner indices disagree")
        value = item[0]
    elif kind is K.RESOLUTION:
        value = EvidenceRepository(store).stored_resolution(identity)
    elif kind is K.EVENT_RESOLUTION:
        value = EvidenceRepository(store).stored_event_run(identity)
    elif kind is K.EXTRACTION:
        value = TextRepository(store).run(identity)
    elif kind is K.GRAPH:
        value = GraphRepository(store).snapshot(identity)
    elif kind is K.PROPAGATION:
        value = GraphRepository(store).propagation(identity)
    elif kind is K.INTELLIGENCE:
        value = IntelligenceRepository(store).get(identity)
    elif kind is K.STUDY:
        value = StudyRepository(store).get(identity)
    elif kind is K.IMPACT:
        value = ImpactRepository(store).get(identity)
    elif kind is K.ANALOGUE:
        value = AnalogueRepository(store).get(identity)
    elif kind is K.REACTION:
        value = AnalogueRepository(store).reaction(identity)
    elif kind is K.INTERACTION:
        value = InteractionRepository(store).context(identity)
    elif kind is K.RELIABILITY:
        value = ReliabilityRepository(store).artifact(identity, SourceReliabilityResult)
    elif kind is K.LATENCY:
        value = ReliabilityRepository(store).artifact(identity, InformationLatency)
    elif kind is K.COVERAGE:
        value = ReliabilityRepository(store).artifact(identity, SourceCoverageResult)
    else:
        value = DatasetRepository(store).manifest(identity)
    if value is None:
        raise PersistenceError("retained service owner unavailable")
    verify_versions(value)
    return value


def verify_versions(value: object) -> None:
    """Existing public schemas and known release versions only, including dependencies."""
    supported = {f"0.{n}.0" for n in range(1, 10)} | {f"1.{n}.0" for n in range(8)} | {"2.0.0"}
    if is_dataclass(value) and not isinstance(value, type):
        for f in fields(value):
            item = getattr(value, f.name)
            if (f.name == "schema_version" and item != "1.0") or (
                f.name == "engine_version" and item not in supported
            ):
                raise ValidationError("unsupported retained component version")
            verify_versions(item)
    elif isinstance(value, (tuple, frozenset)):
        for item in value:
            verify_versions(item)


def event_summary(event: CanonicalEvent) -> EventSummary:
    def clock(value: int | None) -> TimeValue | None:
        return None if value is None else exact(value)

    facts = {f.name: f.value for f in event.facts}
    scheduled_date = facts.get("scheduled_date")
    scheduled = clock(event.scheduled_for_ns)
    if scheduled is None and isinstance(scheduled_date, str):
        scheduled = TimeValue(TimePrecision.DATE_ONLY, scheduled_date)
    return EventSummary(
        event.event_id,
        event.revision_id,
        event.revision.number,
        event.identity.event_type.code,
        event.identity.scope,
        event.lifecycle,
        event.confirmation,
        InformationClocks(
            valid=clock(event.effective_at_ns),
            publication=clock(event.published_at_ns),
            observed=clock(event.source_observed_at_ns),
            ingested=clock(event.ingested_at_ns),
        ),
        event.scheduled_for_ns,
        tuple(sorted(c.value for c in event.source_classes)),
        tuple(sorted(q.value for q in event.quality_flags)),
        scheduled,
    )


@dataclass(frozen=True)
class Projection:
    component: C
    known: int
    minimum_available: int
    scopes: tuple[EventScope, ...]
    group: str
    ordinal: int = 0
    provenance: tuple[ProvenancePin, ...] = ()
    state: ComponentState = ComponentState.AVAILABLE
    quality: tuple[str, ...] = ()
    valid_at: int | None = None
    event: EventSummary | None = None


def _scope(store: SQLiteEventStore, event_id: str, known: int) -> tuple[EventScope, ...]:
    event = store.get_event(event_id, known)
    if event is None:
        # Event identity (including scope) is immutable. Historical EIE-7 facts
        # may have been acquired later than their source knowledge clock.
        revisions = store.get_revisions(event_id)
        if not revisions:
            raise PersistenceError("service component event binding unavailable")
        event = revisions[0]
    return (event.identity.scope,)


def _nodes(store: SQLiteEventStore, ids: tuple[str, ...], known: int) -> tuple[EventScope, ...]:
    scopes = set()
    for identity in ids:
        node = GraphRepository(store).node(identity, known)
        if node is None:
            raise PersistenceError("service graph identity unavailable")
        kind = {
            GraphNodeType.ISSUER: ScopeKind.COMPANY,
            GraphNodeType.SECURITY: ScopeKind.SECURITY,
            GraphNodeType.LISTING: ScopeKind.SECURITY,
            GraphNodeType.ETF: ScopeKind.SECURITY,
            GraphNodeType.INDUSTRY: ScopeKind.INDUSTRY,
            GraphNodeType.SECTOR: ScopeKind.SECTOR,
            GraphNodeType.INDEX: ScopeKind.INDEX,
        }.get(node.node_type, ScopeKind.MARKET)
        key = (
            getattr(node.foundation, "security_id", identity)
            if node.node_type is GraphNodeType.LISTING
            else identity
        )
        scopes.add(EventScope(kind, () if kind is ScopeKind.MARKET else (key,)))
    return tuple(sorted(scopes, key=lambda s: (s.kind.value, s.identifiers)))


def project(store: SQLiteEventStore, kind: K, value: Detail, identity: str) -> Projection:
    scopes: tuple[EventScope, ...]
    pins: tuple[ProvenancePin, ...]
    if isinstance(value, GraphNode):
        from eie.entities.models import Security
        from eie.entities.sec import SecListingRevision

        scope_set = set(_nodes(store, (value.node_id,), value.known_from_ns))
        if isinstance(value.foundation, (Security, SecListingRevision)):
            scope_set.add(EventScope(ScopeKind.COMPANY, (value.foundation.issuer_id,)))
        if isinstance(value.foundation, SecListingRevision):
            scope_set.add(EventScope(ScopeKind.SYMBOL, (value.foundation.ticker,)))
        scopes = tuple(sorted(scope_set, key=lambda s: (s.kind.value, s.identifiers)))
        return Projection(
            C.IDENTITY, value.known_from_ns, value.known_from_ns, scopes, value.node_id
        )
    if isinstance(value, CanonicalEvent):
        return Projection(
            C.EVENTS,
            value.revision.available_from_ns,
            value.revision.available_from_ns,
            (value.identity.scope,),
            value.event_id,
            value.revision.number,
            event=event_summary(value),
        )
    if isinstance(value, (ScheduleRevision, CorporateEvent)):
        event_id = (
            value.event.event_id
            if isinstance(value, ScheduleRevision)
            else canonical_identity(value.identity).event_id
        )
        known = (
            value.event.revision.available_from_ns
            if isinstance(value, ScheduleRevision)
            else value.revision.available_from_ns
        )
        return Projection(
            C.SCHEDULE if isinstance(value, ScheduleRevision) else C.CORPORATE,
            known,
            known,
            _scope(store, event_id, known),
            event_id,
            value.event.revision.number
            if isinstance(value, ScheduleRevision)
            else value.revision.number,
        )
    if isinstance(value, FilingMetadata):
        scope = (
            EventScope(ScopeKind.SEC_FILER, (value.filer_id,))
            if value.issuer_id is None
            else EventScope(ScopeKind.COMPANY, (value.issuer_id,))
        )
        return Projection(
            C.FILINGS,
            value.available_from_ns,
            value.available_from_ns,
            (scope,),
            value.filing_id,
            value.revision_number,
        )
    if isinstance(value, (ActualObservation, EstimateObservation, GuidanceObservation)):
        known = value.source.available_from_ns
        return Projection(
            C.EXPECTATIONS,
            known,
            known,
            _scope(store, value.context.event_id, known),
            getattr(value, "observation_id", identity),
        )
    if isinstance(value, RetainedExpectation):
        native = value.value
        return Projection(
            C.EXPECTATIONS,
            native.as_of_ns,
            native.as_of_ns,
            _scope(store, native.context.event_id, native.as_of_ns),
            fingerprint(
                "eie:service:expectation-group:v1",
                (native.context, value.provider_id, native.configuration_fingerprint),
            ),
            provenance=(
                ProvenancePin(
                    "POLICY",
                    native.configuration_fingerprint,
                    native.configuration_fingerprint,
                    native.as_of_ns,
                ),
            ),
        )
    if isinstance(value, SurpriseObservation):
        return Projection(
            C.EXPECTATIONS,
            value.as_of_ns,
            value.as_of_ns,
            _scope(store, value.context.event_id, value.as_of_ns),
            value.context.event_id + ":" + value.context.measurement_id.code,
            provenance=(
                ProvenancePin(
                    "POLICY",
                    value.configuration_fingerprint,
                    value.configuration_fingerprint,
                    value.as_of_ns,
                ),
            ),
        )
    if isinstance(value, CanonicalClaimResolution):
        evidence = EvidenceRepository(store)
        scope_set = {
            s
            for i in value.alternative_ids
            for a in (evidence.assertion(i),)
            if a.event_id is not None
            for s in _scope(store, a.event_id, value.known_as_of_ns)
        }
        if not scope_set:
            raise ValidationError("claim resolution requires a retained scoped event")
        return Projection(
            C.EVIDENCE,
            value.known_as_of_ns,
            value.known_as_of_ns,
            tuple(sorted(scope_set, key=lambda s: (s.kind.value, s.identifiers))),
            fingerprint(
                "eie:service:claim-group:v1", (value.claim_id, value.dataset_id, value.policy)
            ),
            state=ComponentState.CONFLICTED if value.conflicts else ComponentState.AVAILABLE,
            valid_at=value.valid_at_ns,
        )
    if isinstance(value, EventResolutionRun):
        repo = EvidenceRepository(store)
        scope_set = {
            s
            for i in value.descriptor_ids
            for d in (repo.descriptor(i),)
            for s in _scope(store, d.event_id, value.known_as_of_ns)
        }
        return Projection(
            C.EVIDENCE,
            value.known_as_of_ns,
            value.known_as_of_ns,
            tuple(sorted(scope_set, key=lambda s: (s.kind.value, s.identifiers))),
            fingerprint("eie:service:alias-group:v1", (value.dataset_id, value.policy)),
            quality=("TRUNCATED",) if value.truncated_event_ids else (),
        )
    if isinstance(value, ExtractionRun):
        scope_set = {
            s for link in value.accepted for s in _scope(store, link.event_id, value.available_ns)
        }
        if not scope_set:
            scope_set = {EventScope(ScopeKind.MARKET)}
        doc = TextRepository(store).document(value.document_id)
        return Projection(
            C.EXTRACTION,
            value.available_ns,
            value.available_ns,
            tuple(sorted(scope_set, key=lambda s: (s.kind.value, s.identifiers))),
            value.document_id,
            provenance=(
                ProvenancePin(
                    "SOURCE",
                    doc.document_id,
                    doc.document_id.removeprefix("tdoc_"),
                    doc.raw.ingested_at_ns,
                ),
            ),
            state=ComponentState.INCOMPLETE
            if value.failures or not value.accepted
            else ComponentState.AVAILABLE,
        )
    if isinstance(value, (GraphSnapshot, PropagationResult)):
        roots = (
            value.roots
            if isinstance(value, GraphSnapshot)
            else tuple(b.node_id for b in value.bindings)
        )
        scopes = _nodes(store, roots, value.known_as_of_ns)
        return Projection(
            C.GRAPH,
            value.known_as_of_ns,
            value.known_as_of_ns,
            scopes,
            fingerprint("eie:service:graph-group:v1", (kind, roots, value.traversal, value.policy))
            if isinstance(value, GraphSnapshot)
            else identity,
            valid_at=value.valid_at_ns,
            quality=("TRUNCATED",) if isinstance(value, GraphSnapshot) and value.truncated else (),
        )
    if isinstance(value, EventIntelligenceResult):
        r = value.manifest.request
        pins = (
            ProvenancePin("POLICY", r.policy_id, value.policy_fingerprint, r.available_ns),
            ProvenancePin(
                "DATASET", r.dataset_id, value.manifest.dataset_fingerprint, r.available_ns
            ),
        )
        if value.manifest.build_id is not None:
            build = DatasetRepository(store).manifest(value.manifest.build_id)
            if build is None:
                raise PersistenceError("intelligence build missing")
            pins += (
                ProvenancePin(
                    "BUILD",
                    value.manifest.build_id,
                    build.content_fingerprint,
                    build.completed_at_ns,
                ),
            )
        return Projection(
            C.INTELLIGENCE,
            r.known_as_of_ns,
            r.available_ns,
            _scope(store, value.event_id, r.known_as_of_ns),
            fingerprint(
                "eie:service:intelligence-group:v1", (value.event_id, r.dataset_id, r.policy_id)
            ),
            provenance=tuple(pins),
            quality=value.quality,
        )
    if isinstance(value, (EventStudyResult, EventImpactResult)):
        study = value if isinstance(value, EventStudyResult) else value.inputs.study
        d = study.definition
        manifest = DatasetRepository(store).manifest(d.event_build_id)
        if manifest is None:
            raise PersistenceError("study build unavailable")
        market = study.market.identity
        pins = (
            ProvenancePin(
                "BUILD", d.event_build_id, manifest.content_fingerprint, manifest.completed_at_ns
            ),
            ProvenancePin(
                "MARKET_DATASET", market.dataset_id, market.dataset_fingerprint, study.available_ns
            ),
        )
        markets = [study.market, *(b.data for b in study.benchmarks if b.data is not None)]
        if isinstance(value, EventImpactResult):
            markets.extend(
                [
                    value.inputs.target,
                    *(c.market for c in value.inputs.controls if c.market is not None),
                ]
            )
            if value.inputs.benchmark is not None:
                markets.append(value.inputs.benchmark)
        pins += tuple(
            ProvenancePin(
                "MARKET_DATASET",
                m.identity.dataset_id,
                m.identity.dataset_fingerprint,
                value.available_ns,
            )
            for m in markets
            if isinstance(m, MarketSlice)
        )
        if isinstance(value, EventImpactResult):
            pins += tuple(
                ProvenancePin(
                    "FACTOR_DATASET", f.dataset_id, f.dataset_fingerprint, value.available_ns
                )
                for f in value.inputs.factors
            )
        pins = tuple(
            sorted(
                set(pins),
                key=lambda pin: (pin.kind, pin.identity, pin.fingerprint, pin.available_ns),
            )
        )
        return Projection(
            C.STUDIES if isinstance(value, EventStudyResult) else C.IMPACT,
            value.available_ns,
            max(value.available_ns, manifest.completed_at_ns),
            (d.event.identity.scope,),
            identity,
            provenance=pins,
            quality=tuple(sorted(q.value for q in value.quality)),
        )
    if isinstance(value, AnalogueResult):
        query_0 = value.query
        row_0 = AnalogueRepository(store).require_feature(query_0.target_row_id)
        return Projection(
            C.ANALOGUES,
            query_0.known_as_of_ns,
            query_0.available_ns,
            _scope(store, row_0.event_id, query_0.known_as_of_ns),
            fingerprint(
                "eie:service:analogue-group:v1",
                (
                    row_0.event_id,
                    query_0.dataset_id,
                    query_0.policy_id,
                    query_0.response,
                    query_0.top_k,
                    query_0.earliest_ns,
                ),
            ),
            provenance=(
                ProvenancePin(
                    "POLICY", query_0.policy_id, value.policy_fingerprint, query_0.available_ns
                ),
            ),
            quality=value.quality,
        )
    if isinstance(value, MarketReactionSurprise):
        result = AnalogueRepository(store).get(value.analogue_result_id)
        if result is None:
            raise PersistenceError("reaction analogue unavailable")
        row_1 = AnalogueRepository(store).require_feature(result.query.target_row_id)
        return Projection(
            C.REACTION,
            value.available_ns,
            value.available_ns,
            _scope(store, row_1.event_id, value.available_ns),
            value.analogue_result_id,
            provenance=(
                ProvenancePin(
                    "ANALOGUE",
                    value.analogue_result_id,
                    value.analogue_fingerprint,
                    result.query.available_ns,
                ),
            ),
        )
    if isinstance(value, InteractionContext):
        query_2 = value.query
        row_2 = InteractionRepository(store).event(value.target_row_id)
        return Projection(
            C.INTERACTIONS,
            query_2.known_as_of_ns,
            query_2.known_as_of_ns,
            _scope(store, row_2.event_id, query_2.known_as_of_ns),
            fingerprint(
                "eie:service:interaction-group:v1",
                (
                    row_2.event_id,
                    query_2.dataset_id,
                    query_2.policy_id,
                    query_2.graph_id,
                    query_2.event_resolution_id,
                    query_2.collision_types,
                ),
            ),
            quality=tuple(sorted(f.value for f in value.quality)),
        )
    if isinstance(value, SourceReliabilityResult):
        query_3 = value.query
        scopes = (
            (EventScope(ScopeKind.COMPANY, (query_3.scope_id,)),)
            if query_3.scope_id is not None and query_3.scope_id.startswith("iss_")
            else (EventScope(ScopeKind.MARKET),)
        )
        return Projection(
            C.SOURCE_RELIABILITY,
            query_3.known_as_of_ns,
            query_3.known_as_of_ns,
            scopes,
            fingerprint(
                "eie:service:reliability-group:v1",
                (
                    query_3.dataset_id,
                    query_3.provider,
                    query_3.resource_id,
                    query_3.policy,
                    query_3.event_type,
                    query_3.field,
                    query_3.scope_id,
                    query_3.kind,
                    query_3.coverage_universe_id,
                ),
            ),
            provenance=(
                ProvenancePin(
                    "POLICY",
                    query_3.policy.artifact_id,
                    query_3.policy.fingerprint,
                    query_3.known_as_of_ns,
                ),
            ),
            quality=tuple(sorted(f.value for f in value.quality)),
        )
    if isinstance(value, InformationLatency):
        row_4 = ReliabilityRepository(store).row(value.row_id)
        scopes = (
            (EventScope(ScopeKind.COMPANY, (row_4.scope_id,)),)
            if row_4.scope_id.startswith("iss_")
            else (EventScope(ScopeKind.MARKET),)
        )
        return Projection(
            C.LATENCY,
            value.known_as_of_ns,
            value.known_as_of_ns,
            scopes,
            row_4.source_revision_id,
            provenance=(
                ProvenancePin(
                    "SOURCE_REVISION",
                    row_4.source_revision_id,
                    row_4.source_fingerprint,
                    row_4.sample_available_ns,
                ),
            ),
            quality=tuple(sorted(f.value for f in value.quality)),
        )
    if isinstance(value, SourceCoverageResult):
        scopes = (EventScope(ScopeKind.MARKET),)
        if value.universe_id is not None:
            u = ReliabilityRepository(store).artifact(value.universe_id, CoverageUniverse)
            if u.scope_id.startswith("iss_"):
                scopes = (EventScope(ScopeKind.COMPANY, (u.scope_id,)),)
        return Projection(
            C.SOURCE_COVERAGE,
            value.known_as_of_ns,
            value.known_as_of_ns,
            scopes,
            value.universe_id or "NO_UNIVERSE",
            quality=tuple(sorted(f.value for f in value.quality)),
        )
    if isinstance(value, DatasetManifest):
        return Projection(
            C.DATASET,
            value.completed_at_ns,
            value.completed_at_ns,
            (EventScope(ScopeKind.MARKET),),
            identity,
            provenance=(
                ProvenancePin("BUILD", identity, value.content_fingerprint, value.completed_at_ns),
            ),
        )
    raise ValidationError("unsupported service component owner")


def publication(
    store: SQLiteEventStore, catalog: str, kind: K, lookup: tuple[str, ...], available_ns: int
) -> Publication:
    value = read(store, kind, lookup)
    p = project(store, kind, value, lookup[0])
    minimum = p.minimum_available
    if isinstance(value, CanonicalClaimResolution):
        evidence = EvidenceRepository(store)
        for assertion_id in value.alternative_ids:
            assertion = evidence.assertion(assertion_id)
            raw = store.get_observation(assertion.raw_observation_id)
            if raw is None:
                raise PersistenceError("missing claim acquisition")
            lineage = evidence.lineage(assertion.lineage_id)
            minimum = max(minimum, raw.ingested_at_ns, lineage.ingested_at_ns)
            if assertion.event_id is not None:
                revisions = store.get_revisions(assertion.event_id)
                if revisions:
                    minimum = max(minimum, revisions[0].revision.available_from_ns)
    if isinstance(value, EventResolutionRun):
        evidence = EvidenceRepository(store)
        for descriptor_id in value.descriptor_ids:
            revisions = store.get_revisions(evidence.descriptor(descriptor_id).event_id)
            if revisions:
                minimum = max(minimum, revisions[0].revision.available_from_ns)
    if isinstance(value, EventIntelligenceResult) and value.manifest.build_id is not None:
        manifest = DatasetRepository(store).manifest(value.manifest.build_id)
        if manifest is None:
            raise PersistenceError("intelligence build missing")
        minimum = max(minimum, manifest.completed_at_ns)
    if available_ns < minimum:
        raise ValidationError(
            "service publication precedes retained artifact/dependency availability"
        )
    fp = getattr(value, "result_fingerprint", getattr(value, "fingerprint", None))
    if isinstance(value, PropagationResult):
        fp = value.result_id.removeprefix("propagation_")
    if isinstance(value, DatasetManifest):
        fp = value.content_fingerprint
    if isinstance(value, GraphNode):
        fp = value.revision_id.removeprefix("node_rev_")
    if fp is None and kind is K.DOMAIN:
        fp = fingerprint(
            "eie:durable-contract:v1", (f"{type(value).__module__}.{type(value).__name__}", value)
        )
    if not isinstance(fp, str):
        raise ValidationError("owned artifact fingerprint required")
    owner_id = value.revision_id if isinstance(value, CanonicalEvent) else lookup[0]
    ref = ArtifactReference(
        kind,
        owner_id,
        fp,
        p.known,
        available_ns,
        "1.0",
        p.provenance,
        lookup,
        sha256(payload(store, kind, lookup)).hexdigest(),
        p.event,
    )
    return Publication(
        catalog, p.component, ref, p.group, p.ordinal, p.scopes, p.state, p.quality, p.valid_at
    )
