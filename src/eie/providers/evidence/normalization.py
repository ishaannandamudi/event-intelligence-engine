"""Explicit existing-domain bridges; no inference from prose or names."""

from dataclasses import replace
from hashlib import sha256

from eie.archive.models import ArchiveAcquisition
from eie.backfill.models import ProvenanceKind
from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.datasets.models import HistoricalFact
from eie.evidence.claims import (
    Claim,
    ClaimSubject,
    MeasurementSemantics,
    NumericRange,
    TimePrecision,
    TimeValue,
)
from eie.evidence.references import AcquisitionLineage, EvidenceLocator, EvidenceReference
from eie.evidence.schema import ClaimType, Predicate, SubjectKind
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.observations import ActualObservation, EstimateObservation


def field_reference(
    raw_id: str,
    item_id: str,
    content_hash: str,
    parser_version: str,
    adapter_version: str,
    locator: EvidenceLocator,
    normalized_fragment: object,
) -> EvidenceReference:
    """Hash a supplied normalized field, retaining no fragment body."""
    return EvidenceReference(
        raw_id,
        item_id,
        content_hash,
        sha256(canonical_json_bytes(normalized_fragment)).hexdigest(),
        parser_version,
        adapter_version,
        locator,
    )


def numeric_claim(value: ActualObservation | EstimateObservation) -> Claim:
    kind = (
        ClaimType.ESTIMATE_VALUE
        if isinstance(value, EstimateObservation)
        else ClaimType.MACRO_VALUE
        if value.context.measurement_id.code.startswith("MACRO.")
        else ClaimType.ACTUAL_VALUE
    )
    return Claim(
        kind,
        ClaimSubject(SubjectKind.MEASUREMENT, value.context.event_id),
        Predicate.ESTIMATE if isinstance(value, EstimateObservation) else Predicate.ACTUAL,
        value.context,
        value.value.canonical_unit,
        MeasurementSemantics(estimate_kind=value.estimate_type, contributor_id=value.contributor_id)
        if isinstance(value, EstimateObservation)
        else MeasurementSemantics(
            actual_role=value.role, previous_period=value.previous_reference_period
        ),
    )


def guidance_claim(value: GuidanceObservation) -> tuple[Claim, NumericRange]:
    result = NumericRange(value.low, value.high, value.point)
    return Claim(
        ClaimType.GUIDANCE_VALUE,
        ClaimSubject(SubjectKind.MEASUREMENT, value.context.event_id),
        Predicate.GUIDANCE,
        value.context,
        value.unit,
    ), result


def earnings_time_claim(subject: ClaimSubject, schedule: object) -> tuple[Claim, TimeValue]:
    from eie.corporate.earnings import EarningsSchedule
    from eie.corporate.types import SchedulePrecision

    if not isinstance(schedule, EarningsSchedule):
        raise ValidationError("existing typed earnings schedule required")
    precision = {
        SchedulePrecision.EXACT_TIMESTAMP: TimePrecision.EXACT,
        SchedulePrecision.DATE_ONLY: TimePrecision.DATE_ONLY,
        SchedulePrecision.DATE_PLUS_SESSION: TimePrecision.DATE_PLUS_SESSION,
        SchedulePrecision.UNKNOWN: TimePrecision.UNKNOWN,
    }[schedule.precision]
    return Claim(ClaimType.EARNINGS_SCHEDULE, subject, Predicate.SCHEDULED_FOR), TimeValue(
        precision, schedule.scheduled_date, schedule.scheduled_for_ns, schedule.release_timing
    )


def historical_lineage(
    fact: HistoricalFact, build_id: str, settings_fingerprint: str, acquisition: ArchiveAcquisition
) -> AcquisitionLineage:
    observed = acquisition.replayed_at_ns or acquisition.acquired_at_ns
    return AcquisitionLineage(
        fact.raw.observation_id,
        observed,
        max(observed, fact.raw.ingested_at_ns),
        fact.parser_version,
        settings_fingerprint,
        ProvenanceKind.HISTORICALLY_RECONSTRUCTED,
        fact.quality,
        fact.source_available_ns,
        build_id,
        fact.fact_id,
        acquisition.acquisition_id,
    )


def reprocessing_lineage(
    lineage: AcquisitionLineage, parser_version: str, settings_fingerprint: str
) -> AcquisitionLineage:
    return replace(
        lineage, parser_version=parser_version, settings_fingerprint=settings_fingerprint
    )
