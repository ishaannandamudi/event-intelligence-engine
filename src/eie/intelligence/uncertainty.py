"""Eight independent categorical diagnostics; no probability or master confidence."""

from eie.evidence.claims import NumericRange
from eie.evidence.schema import ClaimStatus, ResolutionState
from eie.expectations.units import Quantity
from eie.impact.models import EventImpactResult, FitStatus, ImpactQuality
from eie.intelligence.inputs import IntelligenceInputs
from eie.intelligence.models import EventMateriality, UncertaintyComponent
from eie.intelligence.novelty import PRIMARY_CLASSES, entity_ids
from eie.intelligence.schema import MaterialityCategory
from eie.intelligence.schema import UncertaintyKind as K
from eie.intelligence.schema import UncertaintyState as S
from eie.nlp.schema import ExtractionMethod, RunStatus
from eie.sources.trust import SourceClass


def component(
    kind: K, state: S, reasons: set[str], ids: tuple[str, ...] = ()
) -> UncertaintyComponent:
    return UncertaintyComponent(kind, state, tuple(sorted(reasons)), tuple(sorted(set(ids))))


def impact_uncertainty(impacts: tuple[EventImpactResult, ...]) -> UncertaintyComponent:
    if not impacts:
        return component(K.IMPACT, S.NOT_COMPUTED, {"IMPACT_NOT_COMPUTED"})
    quality = {q for impact in impacts for q in impact.quality}
    reasons = {"EIE9_" + q.value for q in quality}
    reasons.update("FIT_" + i.fit.status.value for i in impacts)
    high = any(i.fit.status is not FitStatus.AVAILABLE for i in impacts) or bool(
        quality
        & {
            ImpactQuality.CONTROL_CONTAMINATION,
            ImpactQuality.TARGET_CONTAMINATION,
            ImpactQuality.MISSING_EVENT_WINDOW_DATA,
            ImpactQuality.EVENT_EXCLUDED,
        }
    )
    state = S.HIGH if high else S.MODERATE if quality else S.LOW
    reasons.add("MODEL_DIAGNOSTICS_NOT_EXTRACTION_CONFIDENCE")
    return component(K.IMPACT, state, reasons, tuple(i.impact_id for i in impacts))


def decompose(
    data: IntelligenceInputs, materiality: EventMateriality
) -> tuple[UncertaintyComponent, ...]:
    facts = data.current
    refs = tuple(f.assertion.observation_id for f in facts)
    classes = {f.assertion.source.source_class for f in facts}
    source_reasons: set[str] = {"TRUST_" + c.value for c in classes}
    if any(f.resolution.conflicts for f in facts):
        source_reasons.add("SOURCE_CONFLICT")
    if any(f.assertion.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN) for f in facts):
        source_reasons.add("SOURCE_RETRACTION_OR_WITHDRAWAL")
    families = {f.assertion.source_family for f in facts}
    if len(families) > 1:
        source_reasons.add("MULTIPLE_SOURCE_FAMILIES")
    if any(f.resolution.summary.confirmation_count for f in facts):
        source_reasons.add("EIE11_CORROBORATION")
    if any(f.assertion.source_family == f.assertion.source.name for f in facts):
        source_reasons.add("SOURCE_FAMILY_DEFAULT_IDENTITY")
    source_state = (
        S.UNKNOWN
        if not facts
        else S.HIGH
        if {"SOURCE_CONFLICT", "SOURCE_RETRACTION_OR_WITHDRAWAL"} & source_reasons
        or SourceClass.UNVERIFIED in classes
        else S.LOW
        if classes <= PRIMARY_CLASSES
        else S.MODERATE
    )
    unresolved = any(d.unresolved_entity for d in data.documents)
    entities = entity_ids(facts)
    entity_state = S.HIGH if unresolved else S.LOW if entities else S.UNKNOWN
    entity_reasons = (
        {"UNRESOLVED_NAMES_OR_ROLES"}
        if unresolved
        else {"EXACT_STABLE_ENTITY_IDS"}
        if entities
        else {"ENTITY_CONTEXT_MISSING"}
    )
    candidates = tuple(c for d in data.documents for c in d.candidates)
    classification_reasons = {"METHOD_" + c.method.value for c in candidates}
    if any(
        d.run.status in (RunStatus.FAILED, RunStatus.REJECTED) or d.run.failures
        for d in data.documents
    ):
        classification_reasons.add("FAILED_EXTRACTION_IS_NOT_NO_EVENT")
    if any(d.run.status is RunStatus.PARTIAL for d in data.documents):
        classification_reasons.add("PARTIAL_DOCUMENT_EXTRACTION")
    classification_state = (
        S.UNKNOWN
        if not candidates
        else S.MODERATE
        if (
            any(c.method is not ExtractionMethod.DETERMINISTIC for c in candidates)
            or any(d.run.status is not RunStatus.SUCCESS for d in data.documents)
        )
        else S.LOW
    )
    classification_reasons.add(
        "SOURCE_VERIFIED_CANDIDATES" if candidates else "TAXONOMY_WITHOUT_EXTRACTION_AUDIT"
    )
    numeric = tuple(f for f in facts if isinstance(f.assertion.value, (Quantity, NumericRange)))
    numeric_reasons = {"SOURCED_NUMERIC_FIELDS"} if numeric else {"NUMERIC_FIELDS_NOT_SUPPLIED"}
    ranges = any(isinstance(f.assertion.value, NumericRange) for f in numeric)
    conflict = any(
        f.resolution.state is not ResolutionState.RESOLVED or f.resolution.conflicts
        for f in numeric
    )
    if ranges:
        numeric_reasons.add("NUMERIC_RANGE_OR_BOUNDS")
    if conflict:
        numeric_reasons.add("NUMERIC_CONFLICT_OR_UNRESOLVED")
    numeric_state = (
        S.UNKNOWN if not numeric else S.HIGH if conflict else S.MODERATE if ranges else S.LOW
    )
    identity_reasons = set(data.identity_reasons) or {"EVENT_RESOLUTION_NOT_COMPUTED"}
    identity_state = (
        S.HIGH
        if {"IDENTITY_UNRESOLVED", "IDENTITY_TRUNCATED"} & identity_reasons
        else S.MODERATE
        if "COARSE_TIME" in identity_reasons
        else S.UNKNOWN
        if "EVENT_RESOLUTION_NOT_COMPUTED" in identity_reasons
        else S.LOW
    )
    materiality_reasons = {
        r
        for c in materiality.components
        for r in c.reasons
        if r
        in {
            "DENOMINATOR_MISSING",
            "DENOMINATOR_UNIT_MISMATCH",
            "DENOMINATOR_NEAR_ZERO",
            "EXPOSURE_NOT_SUPPLIED",
            "RANGE_CROSSES_CATEGORIES",
            "OPEN_RANGE",
            "GUIDANCE_INITIATED",
            "GUIDANCE_WITHDRAWN",
            "GRAPH_CONFLICT",
            "ESTIMATED_EXPOSURE",
            "UNRESOLVED_ENTITY_COUNT_UNKNOWN",
            "NO_UNIT_THRESHOLD_POLICY",
        }
    }
    materiality_reasons.update(materiality.reasons)
    if any(
        "STATUS_" + s in c.reasons
        for c in materiality.components
        for s in (
            "PLANNED",
            "PENDING",
            "RUMORED",
            "ALLEGED",
            "CONDITIONAL",
            "EXPECTED",
            "DENIED",
            "CANCELLED",
            "WITHDRAWN",
        )
    ):
        materiality_reasons.add("NON_FINAL_OR_DISPUTED_STATUS")
    materiality_state = (
        S.UNKNOWN
        if materiality.category is MaterialityCategory.UNKNOWN
        else S.MODERATE
        if materiality_reasons
        else S.LOW
    )
    materiality_reasons.add("EXPLICIT_POLICY_COMPONENTS")
    ready = bool(
        entities and facts and not unresolved and identity_state not in (S.HIGH, S.UNKNOWN)
    )
    result = (
        component(K.SOURCE, source_state, source_reasons or {"SOURCE_NOT_SUPPLIED"}, refs),
        component(K.ENTITY, entity_state, entity_reasons, entities),
        component(
            K.CLASSIFICATION,
            classification_state,
            classification_reasons,
            tuple(c.candidate_id for c in candidates),
        ),
        component(
            K.NUMERIC,
            numeric_state,
            numeric_reasons,
            tuple(f.assertion.observation_id for f in numeric),
        ),
        component(
            K.EVENT_IDENTITY,
            identity_state,
            identity_reasons,
            ()
            if data.manifest.request.event_resolution_id is None
            else (data.manifest.request.event_resolution_id,),
        ),
        component(K.MATERIALITY, materiality_state, materiality_reasons),
        component(
            K.ANALOGUE,
            S.READY_FOR_ANALOGUE_SEARCH if ready else S.INSUFFICIENT_INPUTS,
            {"READINESS_ONLY_NO_ANALOGUE_RETRIEVAL"},
        ),
        impact_uncertainty(data.impacts),
    )
    return tuple(sorted(result, key=lambda c: c.kind.value))
