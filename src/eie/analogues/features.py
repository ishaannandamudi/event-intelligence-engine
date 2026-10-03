"""Build small ex-ante rows from retained EIE-13 artifacts and supplied context."""

from typing import TYPE_CHECKING

from eie.analogues.models import AnaloguePolicy, ContextFeature, FeatureRow, FeatureValue
from eie.analogues.schema import FeatureKind, FeatureSource
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.models import CanonicalEvent
from eie.events.scope import ScopeKind
from eie.evidence.repository import EvidenceRepository, typed
from eie.expectations.surprise import SurpriseObservation
from eie.graph.claims import GraphQuality
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType
from eie.intelligence.models import NoveltyComponent
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.schema import IntelligenceMode

if TYPE_CHECKING:
    from eie.analogues.repository import AnalogueRepository


def graph_context(
    repo: "AnalogueRepository", graph_id: str | None, entities: tuple[str, ...], cutoff_ns: int
) -> tuple[ContextFeature, ...]:
    if graph_id is None:
        return ()
    graph = GraphRepository(repo.store).snapshot(graph_id)
    if graph is None or graph.known_as_of_ns > cutoff_ns or graph.valid_at_ns != cutoff_ns:
        raise ValidationError("graph context requires an exact historical valid/known snapshot")
    if graph.truncated:
        raise ValidationError("truncated graph cannot establish unique analogue context")
    values: dict[str, set[str]] = {}
    kinds = {
        RelationshipType.SECTOR_MEMBER_OF: "sector",
        RelationshipType.INDUSTRY_MEMBER_OF: "industry",
    }
    unsafe = {
        GraphQuality.UNRESOLVED_ENTITY,
        GraphQuality.CONFLICTING_SOURCE,
        GraphQuality.UNKNOWN_VALID_FROM,
        GraphQuality.PARTIAL_SNAPSHOT,
        GraphQuality.CARRIED_SNAPSHOT,
        GraphQuality.COARSE_DATE,
        GraphQuality.SOURCE_RECONSTRUCTION_LIMITATION,
        GraphQuality.TRUNCATED,
    }
    for edge in graph.edges:
        if not edge.active or edge.conflict_reasons or edge.quality & unsafe:
            continue
        if edge.source_node_id in entities:
            key = kinds.get(edge.relationship_type)
            if key is not None:
                values.setdefault(key, set()).add(edge.target_node_id)
            values.setdefault("graph_relationship", set()).add(edge.relationship_type.value)
    return tuple(
        ContextFeature(
            k,
            next(iter(v)),
            graph_id,
            graph.policy.version,
            graph.valid_at_ns,
            graph.known_as_of_ns,
            valid_from_ns=graph.valid_at_ns,
        )
        for k, v in sorted(values.items())
        if len(v) == 1
    )


def build_features(
    repo: "AnalogueRepository",
    intelligence_id: str,
    policy: AnaloguePolicy,
    contexts: tuple[ContextFeature, ...] = (),
    graph_id: str | None = None,
) -> FeatureRow:
    intelligence = IntelligenceRepository(repo.store).get(intelligence_id)
    if intelligence is None or intelligence.mode is not IntelligenceMode.EX_ANTE_INTELLIGENCE:
        raise ValidationError("retained ex-ante intelligence required")
    request = intelligence.manifest.request
    stored_event = repo.store._rows(
        "SELECT payload FROM events WHERE id=? AND revision=?",
        (request.event_id, request.revision_number),
    )
    if not stored_event:
        raise ValidationError("exact target event revision required")
    event = typed(stored_event[0][0], CanonicalEvent)
    # Historical source projection is explicit; actual build/extraction time stays separate.
    entities = tuple(
        sorted(
            set(intelligence.entity_ids)
            | (
                set(event.identity.scope.identifiers)
                if event.identity.scope.kind is ScopeKind.COMPANY
                else set()
            )
        )
    )
    source_available = event.revision.available_from_ns
    build_id = intelligence.manifest.build_id
    reconstruction: set[str] = set()
    if build_id is not None:
        from eie.datasets.repository import DatasetRepository

        manifest = DatasetRepository(repo.store).manifest(build_id)
        assert manifest is not None
        facts = repo.store._rows(
            "SELECT available FROM historical_facts WHERE build_id=? AND logical_id=? "
            "AND kind='CanonicalEvent' AND revision=?",
            (build_id, event.event_id, event.revision.number),
        )
        clocks = {int(r[0]) for r in facts if isinstance(r[0], int)}
        if len(clocks) != 1:
            raise ValidationError("unique historical event availability required")
        source_available = next(iter(clocks))
        reconstruction.add("HISTORICAL_SOURCE_RECONSTRUCTION")
    if request.known_as_of_ns > source_available:
        raise ValidationError("candidate features cannot use post-event intelligence")
    combined = tuple(
        sorted(
            (
                *contexts,
                *graph_context(repo, graph_id, entities, request.known_as_of_ns),
            ),
            key=lambda c: c.dimension,
        )
    )
    if len(combined) > repo.limits.contexts or len({c.dimension for c in combined}) != len(
        combined
    ):
        raise ValidationError("bounded distinct context dimensions required")
    if any(not c.visible(request.known_as_of_ns, source_available) for c in combined):
        raise ValidationError("future or invalid historical context")
    context_map = {c.dimension: c for c in combined}
    evidence = EvidenceRepository(repo.store)
    values = []
    for feature in policy.features:
        value: str | float | bool | None = None
        reason = "FEATURE_NOT_AVAILABLE"
        signature = feature.source.value + ":" + feature.selector
        ids: tuple[str, ...] = (intelligence_id,)
        match feature.source:
            case FeatureSource.EVENT_TYPE:
                value = intelligence.event_type
            case FeatureSource.EVENT_FAMILY:
                value = intelligence.event_family
            case FeatureSource.ENTITY:
                if len(entities) == 1:
                    value = entities[0]
                else:
                    reason = "ENTITY_NOT_UNIQUE"
            case FeatureSource.MATERIALITY:
                if intelligence.materiality.category.value != "UNKNOWN":
                    value = intelligence.materiality.category.value
            case FeatureSource.CONTEXT | FeatureSource.TIMING:
                c = context_map.get(feature.selector)
                if c is not None:
                    value, signature, ids = c.value, signature + ":" + c.unit, (c.fingerprint,)
            case FeatureSource.UNCERTAINTY:
                u = next(
                    (u for u in intelligence.uncertainty if u.kind.value == feature.selector), None
                )
                if u is not None:
                    value = u.state.value
            case FeatureSource.INFORMATION_SURPRISE:
                selected = []
                for identity in request.surprise_ids:
                    stored = evidence._one("ei_information", identity, SurpriseObservation)
                    if stored is None:
                        raise ValidationError(
                            "information feature requires retained EIE-5 artifact"
                        )
                    surprise, scalars = stored
                    if (
                        scalars
                        != (
                            identity,
                            "SURPRISE",
                            event.event_id,
                            surprise.as_of_ns,
                            surprise.result_fingerprint,
                        )
                        or surprise.as_of_ns > request.known_as_of_ns
                    ):
                        raise ValidationError("information feature clock/index mismatch")
                    if feature.measurement == surprise.context.measurement_id.code:
                        selected.append((identity, surprise))
                if len(selected) == 1:
                    identity, surprise = selected[0]
                    value = (
                        None
                        if surprise.normalized_surprise is None
                        else float(surprise.normalized_surprise)
                    )
                    signature += (
                        ":" + str(feature.measurement) + ":" + surprise.configuration_fingerprint
                    )
                    ids = (identity,)
                else:
                    reason = "INFORMATION_MEASUREMENT_MISSING_OR_AMBIGUOUS"
            case FeatureSource.NOVELTY_METRIC | FeatureSource.MATERIALITY_METRIC:
                matches = []
                for component in (
                    intelligence.novelty
                    if feature.source is FeatureSource.NOVELTY_METRIC
                    else intelligence.materiality.components
                ):
                    dimension = (
                        component.dimension.value
                        if isinstance(component, NoveltyComponent)
                        else component.kind.value
                    )
                    for metric in component.metrics:
                        if feature.selector != dimension + "." + metric.name:
                            continue
                        claim = None
                        if feature.measurement is not None:
                            if component.subject_id.startswith("cclaim_"):
                                claim = evidence.claim(component.subject_id)
                            elif component.subject_id.startswith("pclaim_"):
                                claim = evidence.claim(
                                    evidence.assertion(component.subject_id).claim_id
                                )
                            else:
                                continue
                            if (
                                claim.measurement is None
                                or claim.measurement.measurement_id.code != feature.measurement
                            ):
                                continue
                        matches.append((metric, claim))
                if len(matches) == 1:
                    metric, claim = matches[0]
                    value = metric.value
                    reason = metric.reason or reason
                    signature += ":" + fingerprint("eie:analogue-feature-unit:v1", metric.unit)
                    signature += (
                        ":" + str(feature.measurement) + ":" + intelligence.policy_fingerprint
                    )
                else:
                    reason = "METRIC_MISSING_OR_AMBIGUOUS"
        if value is not None:
            valid = (
                isinstance(value, str)
                if feature.kind in (FeatureKind.CATEGORICAL, FeatureKind.ORDINAL)
                else isinstance(value, bool)
                if feature.kind is FeatureKind.BOOLEAN
                else isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            if not valid or (
                feature.kind is FeatureKind.ORDINAL and value not in feature.ordinal_labels
            ):
                raise ValidationError("feature value incompatible with declared comparison")
        values.append(
            FeatureValue(feature.name, value, signature, ids, reason if value is None else None)
        )
    measurements = tuple(
        str(r[0])
        for r in repo.store._rows(
            "SELECT DISTINCT measurement FROM ei_measurements WHERE result=? ORDER BY measurement",
            (intelligence_id,),
        )
    )
    graph_quality = set()
    if graph_id is not None:
        snapshot = GraphRepository(repo.store).snapshot(graph_id)
        assert snapshot is not None
        graph_quality = {"GRAPH_" + q.value for edge in snapshot.edges for q in edge.quality}
    return FeatureRow(
        event.event_id,
        event.revision.number,
        event.revision_id,
        event.result_fingerprint,
        request.dataset_id,
        intelligence.manifest.dataset_fingerprint,
        build_id,
        intelligence.event_type,
        intelligence.event_family,
        entities,
        intelligence.manifest.excluded_event_ids,
        source_available,
        request.known_as_of_ns,
        request.available_ns,
        intelligence_id,
        intelligence.result_fingerprint,
        policy.policy_id,
        tuple(values),
        combined,
        None
        if "sector" not in context_map or context_map["sector"].value is None
        else str(context_map["sector"].value),
        None
        if "industry" not in context_map or context_map["industry"].value is None
        else str(context_map["industry"].value),
        intelligence.materiality.category.value,
        measurements,
        graph_id,
        tuple(sorted(reconstruction)),
        request.surprise_ids,
        tuple(sorted(set(intelligence.quality) | graph_quality)),
    )
