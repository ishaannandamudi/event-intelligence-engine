"""Source projections verify retained owners once, at index construction."""

from dataclasses import fields, replace

from eie.backfill.models import ProvenanceKind
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.models import CanonicalEvent
from eie.evidence.claims import (
    ClaimValue,
    NumericRange,
    ProviderClaimObservation,
    RelationshipValue,
    TimeValue,
)
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus, Predicate
from eie.expectations.observations import ActualRole
from eie.expectations.units import Quantity
from eie.filings.models import FilingMetadata
from eie.nlp.documents import ExtractionAcquisition
from eie.reliability.clocks import exact
from eie.reliability.corrections import claim_behavior, event_behavior
from eie.reliability.models import InformationClocks, ProcessingTiming, SourceHistoryRow
from eie.reliability.schema import RevisionBehavior, SubjectType
from eie.sources.models import RawSourceObservation


def value_fingerprint(value: ClaimValue) -> str:
    # Match EIE-11 compatibility while excluding audit-only source metadata.
    if isinstance(value, Quantity):
        content: object = (value.canonical_unit, float(value.canonical_value))
    elif isinstance(value, NumericRange):
        content = tuple(
            None if v is None else value_fingerprint(v)
            for v in (value.lower, value.upper, value.point)
        )
    elif isinstance(value, FilingMetadata):
        ignored = {
            "source_reference",
            "source_observation_id",
            "available_from_ns",
            "revision_number",
            "quality_flags",
        }
        content = tuple(
            (f.name, getattr(value, f.name)) for f in fields(value) if f.name not in ignored
        )
    elif isinstance(value, RelationshipValue) and value.exposure is not None:
        content = replace(
            value, exposure=replace(value.exposure, source_reference="compact-reference")
        )
    else:
        content = value
    return fingerprint("eie:reliability-value:v1", content)


def timing_clocks(
    clocks: InformationClocks, timing: ProcessingTiming | None, subject: str, source_fp: str
) -> InformationClocks:
    if timing is None:
        return clocks
    if (timing.subject_id, timing.source_fingerprint) != (subject, source_fp):
        raise ValidationError("processing timing must pin the exact retained source revision")
    if clocks.normalized is not None and timing.normalized not in (None, clocks.normalized):
        raise ValidationError("processing timing contradicts extraction availability")
    return replace(
        clocks,
        normalized=timing.normalized or clocks.normalized,
        snapshot=timing.snapshot,
        coarse_timezone=timing.coarse_timezone or clocks.coarse_timezone,
    )


def from_claim(
    repo: EvidenceRepository,
    assertion: ProviderClaimObservation,
    timing: ProcessingTiming | None = None,
) -> SourceHistoryRow:
    claim = repo.claim(assertion.claim_id)
    lineage = repo.lineage(assertion.lineage_id)
    raw = repo.store.get_observation(assertion.raw_observation_id)
    if raw is None:
        raise ValidationError("reliability requires retained source observation")
    event = (
        None
        if assertion.event_id is None
        else repo.store.get_event(
            assertion.event_id, max(assertion.known_from_ns, lineage.ingested_at_ns)
        )
    )
    publication = (
        assertion.value
        if isinstance(assertion.value, TimeValue) and claim.predicate is Predicate.PUBLISHED_AT
        else exact(raw.published_at_ns)
    )
    normalized = (
        exact(lineage.extraction_available_ns)
        if isinstance(lineage, ExtractionAcquisition)
        else None
    )
    clocks = InformationClocks(
        exact(assertion.valid_from_ns),
        publication,
        exact(lineage.source_available_ns),
        exact(lineage.observed_at_ns),
        exact(lineage.ingested_at_ns),
        normalized,
    )
    source_fp = fingerprint("eie:reliability-assertion-source:v1", assertion)
    clocks = timing_clocks(clocks, timing, assertion.observation_id, source_fp)
    return SourceHistoryRow(
        assertion.dataset_id,
        SubjectType.CLAIM,
        assertion.claim_id,
        assertion.thread_id,
        assertion.observation_id,
        source_fp,
        raw.observation_id,
        lineage.lineage_id,
        assertion.source,
        assertion.resource_id,
        assertion.source_family,
        event.identity.event_type.code if event is not None else claim.claim_type.value,
        claim.predicate.value,
        claim.subject.identity,
        assertion.number,
        assertion.known_from_ns,
        max(
            assertion.known_from_ns,
            lineage.ingested_at_ns,
            0 if normalized is None else normalized.timestamp_ns or 0,
            0 if timing is None else timing.available_ns,
        ),
        max(
            assertion.known_from_ns,
            lineage.ingested_at_ns,
            0 if normalized is None else normalized.timestamp_ns or 0,
        ),
        clocks,
        claim_behavior(
            assertion.status,
            expected=claim.measurement_semantics is not None
            and claim.measurement_semantics.actual_role is ActualRole.REVISED_PREVIOUS,
        ),
        value_fingerprint(assertion.value),
        assertion.valid_to_ns,
        assertion.previous_id,
        None if timing is None else timing.artifact_id,
        assertion.provenance is not ProvenanceKind.LIVE_OBSERVED,
        assertion.status not in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN),
        assertion.value if isinstance(assertion.value, TimeValue) else None,
    )


def from_event(
    repo: EvidenceRepository,
    dataset: str,
    event: CanonicalEvent,
    resource: str,
    family: str,
    raw_id: str,
    timing: ProcessingTiming | None = None,
) -> SourceHistoryRow:
    if raw_id not in event.evidence_observation_ids:
        raise ValidationError("event source must be retained evidence")
    raw = repo.store.get_observation(raw_id)
    if raw is None:
        raise ValidationError("event source observation missing")
    clocks = timing_clocks(
        InformationClocks(
            exact(event.effective_at_ns),
            exact(raw.published_at_ns),
            None,
            exact(raw.source_observed_at_ns),
            exact(raw.ingested_at_ns),
        ),
        timing,
        event.revision_id,
        event.result_fingerprint,
    )
    return SourceHistoryRow(
        dataset,
        SubjectType.EVENT,
        event.event_id,
        fingerprint(
            "eie:reliability-event-thread:v1", (dataset, raw.source.name, resource, event.event_id)
        ),
        event.revision_id,
        event.result_fingerprint,
        raw_id,
        None,
        raw.source,
        resource,
        family,
        event.identity.event_type.code,
        "EVENT",
        event.identity.scope.identifiers[0]
        if event.identity.scope.identifiers
        else event.identity.scope.kind.value,
        event.revision.number,
        event.revision.available_from_ns,
        max(
            event.revision.available_from_ns,
            raw.ingested_at_ns,
            0 if timing is None else timing.available_ns,
        ),
        max(event.revision.available_from_ns, raw.ingested_at_ns),
        clocks,
        event_behavior(
            event.revision.kind, macro=event.identity.event_type.code.startswith("MACRO.")
        ),
        event.event_payload_fingerprint,
        previous_source_id=None
        if event.revision.number == 1
        else "rev_"
        + fingerprint("eie:event-revision:v1", (event.event_id, event.revision.number - 1)),
        timing_id=None if timing is None else timing.artifact_id,
    )


def from_observation(
    dataset: str,
    raw: RawSourceObservation,
    resource: str,
    family: str,
    timing: ProcessingTiming | None = None,
) -> SourceHistoryRow:
    clocks = timing_clocks(
        InformationClocks(
            publication=exact(raw.published_at_ns),
            observed=exact(raw.source_observed_at_ns),
            ingested=exact(raw.ingested_at_ns),
        ),
        timing,
        raw.observation_id,
        raw.result_fingerprint,
    )
    return SourceHistoryRow(
        dataset,
        SubjectType.OBSERVATION,
        raw.observation_id,
        raw.observation_id,
        raw.observation_id,
        raw.result_fingerprint,
        raw.observation_id,
        None,
        raw.source,
        resource,
        family,
        "OBSERVATION",
        "OBSERVATION",
        "UNSPECIFIED",
        1,
        raw.ingested_at_ns,
        max(raw.ingested_at_ns, 0 if timing is None else timing.available_ns),
        raw.ingested_at_ns,
        clocks,
        RevisionBehavior.ORIGINAL,
        raw.payload_hash,
        timing_id=None if timing is None else timing.artifact_id,
    )
