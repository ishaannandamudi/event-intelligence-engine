"""Provider-supplied consensus and explicit within-provider analyst aggregation."""

from dataclasses import dataclass, replace
from enum import StrEnum
from statistics import fmean, median, stdev

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.time import require_ns
from eie.expectations.models import MeasurementContext, flags
from eie.expectations.observations import EstimateKind, EstimateObservation
from eie.expectations.queries import EstimateAvailability, EstimateCoverage
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.units import CanonicalUnit, Number, number
from eie.sources.models import SourceHealth, SourceHealthState
from eie.sources.trust import SourcePrecedencePolicy


class ConsensusOrigin(StrEnum):
    PROVIDER = "PROVIDER"
    ENGINE = "ENGINE"


class AggregationMethod(StrEnum):
    MEAN = "MEAN"
    MEDIAN = "MEDIAN"
    HIGH = "HIGH"
    LOW = "LOW"
    COUNT = "COUNT"


@dataclass(frozen=True, slots=True)
class ConsensusSnapshot:
    context: MeasurementContext
    as_of_ns: int
    origin: ConsensusOrigin
    method: AggregationMethod
    provider_id: str | None
    value: Number | None
    unit: CanonicalUnit | None
    contributor_count: int | None
    high: Number | None
    low: Number | None
    dispersion: Number | None
    interquartile_range: Number | None
    age_ns: int | None
    point_in_time: bool
    inputs: tuple[EstimateObservation, ...]
    provider_claims: tuple[EstimateObservation, ...]
    coverage: tuple[EstimateCoverage, ...]
    health: tuple[tuple[str, SourceHealth], ...]
    availability: EstimateAvailability
    quality_flags: frozenset[QualityFlag]
    reason_codes: tuple[str, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns)
        flags(self.quality_flags)
        if (
            not isinstance(self.context, MeasurementContext)
            or not isinstance(self.origin, ConsensusOrigin)
            or not isinstance(self.method, AggregationMethod)
            or not isinstance(self.availability, EstimateAvailability)
        ):
            raise ValidationError("typed consensus contract required")
        for value in (self.value, self.high, self.low, self.dispersion, self.interquartile_range):
            if value is not None:
                number(value)
        if self.age_ns is not None:
            require_ns(self.age_ns)
        if any(
            v.source.available_from_ns > self.as_of_ns
            for v in (*self.inputs, *self.provider_claims)
        ) or any(v.available_from_ns > self.as_of_ns for v in self.coverage):
            raise TemporalConsistencyError("future consensus evidence")
        if (self.value is None) != (self.unit is None) or (
            self.value is not None and not self.inputs
        ):
            raise ValidationError("consensus value requires unit and evidence")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValidationError("consensus reasons must be sorted unique")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:consensus-snapshot:v1", self)

    @property
    def spread(self) -> Number | None:
        if self.high is None or self.low is None:
            return None
        try:
            value = self.high - self.low
            number(value)
            return value
        except (ArithmeticError, ValidationError):
            return None


def consensus_snapshot(
    context: MeasurementContext,
    observations: tuple[EstimateObservation, ...],
    as_of_ns: int,
    settings: ExpectationsSettings,
    precedence: SourcePrecedencePolicy,
    configuration_fingerprint: str,
    coverage: tuple[EstimateCoverage, ...] = (),
    health: tuple[tuple[str, SourceHealth], ...] = (),
    *,
    provider_id: str | None = None,
    method: AggregationMethod = AggregationMethod.MEAN,
    origin: ConsensusOrigin = ConsensusOrigin.PROVIDER,
) -> ConsensusSnapshot:
    require_ns(as_of_ns)
    if not isinstance(method, AggregationMethod) or not isinstance(origin, ConsensusOrigin):
        raise ValidationError("typed consensus selection required")
    eligible = tuple(
        v
        for v in observations
        if v.context == context
        and v.source.available_from_ns <= as_of_ns
        and (provider_id is None or v.source.provider_id == provider_id)
    )
    reasons: set[str] = set()
    quality: set[QualityFlag] = set()
    inputs: tuple[EstimateObservation, ...] = ()
    claims: tuple[EstimateObservation, ...] = ()
    value: Number | None = None
    unit: CanonicalUnit | None = None
    count: int | None = None
    high = low = dispersion = iqr = None
    selected_provider = provider_id
    if origin is ConsensusOrigin.PROVIDER:
        kind = EstimateKind(f"PROVIDER_CONSENSUS_{method.value}")
        latest: dict[str, EstimateObservation] = {}
        for row in sorted(eligible, key=lambda v: (v.source.available_from_ns, v.stream_id)):
            if row.estimate_type is kind:
                latest[row.source.provider_id] = row
        claims = tuple(latest[key] for key in sorted(latest))
        if claims:
            order = precedence.order_for(f"consensus.{context.measurement_id.code.lower()}")
            selected = min(
                claims, key=lambda v: (order.index(v.source.source_class), v.source.provider_id)
            )
            inputs = (selected,)
            selected_provider = selected.source.provider_id
            value, unit, count = (
                selected.value.canonical_value,
                selected.value.canonical_unit,
                selected.contributor_count,
            )
            if len({(v.value.canonical_value, v.value.canonical_unit) for v in claims}) > 1:
                reasons.add("CONFLICTING_PROVIDERS")
                quality.add(QualityFlag.CONFLICTING_SOURCES)
            for extra_kind in (
                EstimateKind.PROVIDER_CONSENSUS_HIGH,
                EstimateKind.PROVIDER_CONSENSUS_LOW,
                EstimateKind.PROVIDER_CONSENSUS_COUNT,
            ):
                extras = tuple(
                    v
                    for v in eligible
                    if v.source.provider_id == selected_provider and v.estimate_type is extra_kind
                )
                if extras:
                    extra = max(extras, key=lambda v: (v.source.available_from_ns, v.stream_id))
                    if extra_kind is EstimateKind.PROVIDER_CONSENSUS_COUNT:
                        count = int(extra.value.canonical_value)
                    elif extra.value.canonical_unit == unit:
                        if extra_kind is EstimateKind.PROVIDER_CONSENSUS_HIGH:
                            high = extra.value.canonical_value
                        else:
                            low = extra.value.canonical_value
                    else:
                        reasons.add("INCOMPATIBLE_PROVIDER_STATISTIC")
                    inputs = tuple(sorted({*inputs, extra}, key=lambda v: v.stream_id))
    elif provider_id is None:
        reasons.add("ENGINE_CONSENSUS_REQUIRES_PROVIDER")
    else:
        contributors: dict[str, EstimateObservation] = {}
        for row in sorted(eligible, key=lambda v: (v.source.available_from_ns, v.stream_id)):
            if row.estimate_type is EstimateKind.INDIVIDUAL_ANALYST:
                if row.contributor_id is None:
                    reasons.add("CONTRIBUTOR_ID_MISSING")
                    quality.add(QualityFlag.DEGRADED)
                else:
                    contributors[row.contributor_id] = row
        inputs = tuple(contributors[key] for key in sorted(contributors))
        if inputs and len({v.value.canonical_unit for v in inputs}) == 1:
            unit = inputs[0].value.canonical_unit
            values = sorted(v.value.canonical_value for v in inputs)
            count, high, low = len(values), values[-1], values[0]
            try:
                if method is AggregationMethod.MEAN:
                    value = fmean(values)
                elif method is AggregationMethod.MEDIAN:
                    value = median(values)
                elif method is AggregationMethod.HIGH:
                    value = values[-1]
                elif method is AggregationMethod.LOW:
                    value = values[0]
                else:
                    value = len(values)
                if method is AggregationMethod.COUNT:
                    from eie.expectations.units import Dimension

                    unit = CanonicalUnit(Dimension.COUNT)
                dispersion = stdev(values) if len(values) > 1 else None
                if len(values) >= 4:
                    half = len(values) // 2
                    iqr = median(values[-half:]) - median(values[:half])
                number(value)
                for statistic in (dispersion, iqr):
                    if statistic is not None:
                        number(statistic)
            except (ArithmeticError, ValidationError):
                value = unit = dispersion = iqr = None
                high = low = None
                reasons.add("NUMERIC_OVERFLOW")
        elif inputs:
            reasons.add("INCOMPATIBLE_UNITS")
    # The oldest contributing observation governs freshness; a fresh count must
    # not hide a stale central estimate. Age measures elapsed EIE knowledge time.
    age = None if not inputs else as_of_ns - min(v.source.available_from_ns for v in inputs)
    pit = bool(inputs) and all(v.source.vintage_at_ns is not None for v in inputs)
    if inputs and not pit:
        reasons.add("NON_POINT_IN_TIME_PROVENANCE")
        quality.add(QualityFlag.MISSING_TIMESTAMP)
    if value is not None:
        availability = EstimateAvailability.AVAILABLE
        if age is not None and age > settings.staleness_ns:
            availability = EstimateAvailability.STALE_ESTIMATE
            quality.add(QualityFlag.STALE)
    else:
        failed = any(
            h.state
            in (
                SourceHealthState.UNAVAILABLE,
                SourceHealthState.RATE_LIMITED,
                SourceHealthState.SCHEMA_MISMATCH,
                SourceHealthState.DEGRADED,
            )
            for _, h in health
        )
        covered = any(c.covers(context.event_id, context.measurement_id) for c in coverage)
        availability = (
            EstimateAvailability.PROVIDER_UNAVAILABLE
            if failed
            else EstimateAvailability.NO_ESTIMATE_AVAILABLE
            if covered
            else EstimateAvailability.COVERAGE_UNKNOWN
        )
        reasons.add(availability.value)
    for provider, state in health:
        if provider == selected_provider and state.state in (
            SourceHealthState.UNAVAILABLE,
            SourceHealthState.RATE_LIMITED,
            SourceHealthState.SCHEMA_MISMATCH,
            SourceHealthState.DEGRADED,
        ):
            quality.add(QualityFlag.SOURCE_UNAVAILABLE)
    quality.update(flag for v in inputs for flag in v.quality_flags)
    return ConsensusSnapshot(
        context,
        as_of_ns,
        origin,
        method,
        selected_provider,
        value,
        unit,
        count,
        high,
        low,
        dispersion,
        iqr,
        age,
        pit,
        inputs,
        claims,
        coverage,
        tuple((p, replace(h, latency_ns=None, parse_failures=0)) for p, h in health),
        availability,
        frozenset(quality),
        tuple(sorted(reasons)),
        configuration_fingerprint,
    )
