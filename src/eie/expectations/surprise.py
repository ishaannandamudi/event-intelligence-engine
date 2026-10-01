"""Mathematical component surprise with explicit missingness and no sign interpretation."""

from dataclasses import dataclass, replace

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.time import require_ns
from eie.expectations.consensus import ConsensusSnapshot
from eie.expectations.models import MeasurementContext, flags
from eie.expectations.normalization import SurpriseScale
from eie.expectations.observations import ActualObservation
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.units import Dimension, Number, number


@dataclass(frozen=True, slots=True)
class SurpriseObservation:
    context: MeasurementContext
    as_of_ns: int
    actual: ActualObservation | None
    actual_claims: tuple[ActualObservation, ...]
    previous: tuple[ActualObservation, ...]
    revised_previous: tuple[ActualObservation, ...]
    consensus: ConsensusSnapshot
    consensus_cutoff_ns: int | None
    release_at_ns: int | None
    original_actual_available_from_ns: int | None
    raw_surprise: Number | None
    relative_surprise: Number | None
    normalization: SurpriseScale | None
    normalized_surprise: Number | None
    quality_flags: frozenset[QualityFlag]
    reason_codes: tuple[str, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns)
        flags(self.quality_flags)
        if (
            (self.actual is not None and self.actual.source.available_from_ns > self.as_of_ns)
            or any(
                v.source.available_from_ns > self.as_of_ns
                for v in (*self.actual_claims, *self.previous, *self.revised_previous)
            )
            or self.consensus.as_of_ns > self.as_of_ns
        ):
            raise TemporalConsistencyError("future surprise evidence")
        for value in (self.raw_surprise, self.relative_surprise, self.normalized_surprise):
            if value is not None:
                number(value)
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValidationError("surprise reasons must be sorted unique")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:surprise-observation:v1", self)


def calculate_surprise(
    context: MeasurementContext,
    as_of_ns: int,
    actual: ActualObservation | None,
    consensus: ConsensusSnapshot,
    settings: ExpectationsSettings,
    configuration_fingerprint: str,
    *,
    claims: tuple[ActualObservation, ...] = (),
    previous: tuple[ActualObservation, ...] = (),
    revised_previous: tuple[ActualObservation, ...] = (),
    cutoff_ns: int | None = None,
    release_at_ns: int | None = None,
    original_available_ns: int | None = None,
) -> SurpriseObservation:
    reasons: set[str] = set(consensus.reason_codes)
    quality = set(consensus.quality_flags)
    raw = relative = None
    if actual is None:
        reasons.add("ACTUAL_NOT_AVAILABLE")
    elif actual.context != context or consensus.context != context:
        reasons.add("MEASUREMENT_OR_PERIOD_MISMATCH")
    elif consensus.value is None:
        reasons.add("CONSENSUS_MISSING")
    elif not consensus.point_in_time:
        reasons.add("CONSENSUS_VINTAGE_REQUIRED")
    elif actual.value.canonical_unit != consensus.unit:
        reasons.add("INCOMPATIBLE_UNITS")
        quality.add(QualityFlag.INVALID_UNIT)
    else:
        try:
            raw = actual.value.canonical_value - consensus.value
            number(raw)
        except (ArithmeticError, ValidationError):
            raw = None
            reasons.add("NUMERIC_OVERFLOW")
        if settings.allow_relative_surprise and raw is not None:
            eligible_dimension = {
                "EARNINGS.EPS.DILUTED": Dimension.CURRENCY_PER_SHARE,
                "EARNINGS.REVENUE": Dimension.CURRENCY,
            }.get(context.measurement_id.code)
            if (
                eligible_dimension is None
                or actual.value.canonical_unit.dimension is not eligible_dimension
            ):
                reasons.add("RELATIVE_SURPRISE_INELIGIBLE")
            elif abs(consensus.value) < settings.relative_min_abs_consensus:
                reasons.add("RELATIVE_CONSENSUS_NEAR_ZERO")
            else:
                try:
                    relative = raw / abs(consensus.value)
                    number(relative)
                except (ArithmeticError, ValidationError):
                    relative = None
                    reasons.add("RELATIVE_NUMERIC_OVERFLOW")
    if actual is not None:
        quality.update(actual.quality_flags)
    if len({(v.value.canonical_value, v.value.canonical_unit) for v in claims}) > 1:
        quality.add(QualityFlag.CONFLICTING_SOURCES)
        reasons.add("CONFLICTING_ACTUAL_PROVIDERS")
    return SurpriseObservation(
        context,
        as_of_ns,
        actual,
        claims,
        previous,
        revised_previous,
        consensus,
        cutoff_ns,
        release_at_ns,
        original_available_ns,
        raw,
        relative,
        None,
        None,
        frozenset(quality),
        tuple(sorted(reasons)),
        configuration_fingerprint,
    )


def with_normalization(result: SurpriseObservation, scale: SurpriseScale) -> SurpriseObservation:
    reasons = set(result.reason_codes) | set(scale.reason_codes)
    quality = set(result.quality_flags)
    normalized = None
    if "POOLED_PROVIDER_HISTORY" in scale.reason_codes:
        quality.add(QualityFlag.DEGRADED)
    if result.raw_surprise is not None and scale.scale is not None:
        try:
            normalized = result.raw_surprise / scale.scale
            number(normalized)
        except (ArithmeticError, ValidationError):
            normalized = None
            reasons.add("NORMALIZED_NUMERIC_OVERFLOW")
    return replace(
        result,
        normalization=scale,
        normalized_surprise=normalized,
        quality_flags=frozenset(quality),
        reason_codes=tuple(sorted(reasons)),
    )
