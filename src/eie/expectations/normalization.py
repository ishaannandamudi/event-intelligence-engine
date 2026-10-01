"""Reconstructable strict-prior MAD or sample-standard-deviation scales."""

from dataclasses import dataclass
from statistics import median, stdev

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.expectations.consensus import AggregationMethod
from eie.expectations.models import MeasurementContext
from eie.expectations.settings import ExpectationsSettings, ProviderPooling, ScaleMethod
from eie.expectations.units import CanonicalUnit, Number, number


@dataclass(frozen=True, slots=True)
class HistoricalSurprise:
    context: MeasurementContext
    event_type_code: str
    issuer_id: str | None
    unit: CanonicalUnit
    release_at_ns: int
    available_from_ns: int
    provider_id: str
    consensus_method: AggregationMethod
    raw_surprise: Number
    surprise_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.release_at_ns)
        require_ns(self.available_from_ns)
        number(self.raw_surprise)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:historical-surprise:v1", self)


@dataclass(frozen=True, slots=True)
class SurpriseScale:
    context: MeasurementContext
    as_of_ns: int
    method: ScaleMethod
    provider_scope: str
    center: Number | None
    scale: Number | None
    history: tuple[HistoricalSurprise, ...]
    reason_codes: tuple[str, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns)
        for value in (self.center, self.scale):
            if value is not None:
                number(value)
        if self.scale is not None and self.scale <= 0:
            raise ValidationError("normalization scale must be positive")
        if any(
            v.available_from_ns >= self.as_of_ns
            or v.release_at_ns >= self.as_of_ns
            or v.context.event_id == self.context.event_id
            for v in self.history
        ):
            raise ValidationError("normalization history must be strictly prior")
        keys = tuple((v.release_at_ns, v.context.event_id) for v in self.history)
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("normalization history must be sorted unique")

    @property
    def observation_count(self) -> int:
        return len(self.history)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:surprise-scale:v1", self)


def historical_scale(
    context: MeasurementContext,
    history: tuple[HistoricalSurprise, ...],
    cutoff_ns: int,
    unit: CanonicalUnit,
    event_type_code: str,
    issuer_id: str | None,
    provider_id: str,
    method: AggregationMethod,
    settings: ExpectationsSettings,
    configuration_fingerprint: str,
) -> SurpriseScale:
    require_ns(cutoff_ns)
    rows = tuple(
        sorted(
            (
                v
                for v in history
                if v.context.event_id != context.event_id
                and v.release_at_ns < cutoff_ns
                and v.available_from_ns < cutoff_ns
                and v.context.measurement_id == context.measurement_id
                and v.context.release_stage == context.release_stage
                and v.context.seasonal_adjustment == context.seasonal_adjustment
                and v.unit == unit
                and v.event_type_code == event_type_code
                and v.issuer_id == issuer_id
                and v.consensus_method is method
                and (
                    settings.provider_pooling_policy is ProviderPooling.EXPLICIT_POOL
                    or v.provider_id == provider_id
                )
            ),
            key=lambda v: (v.release_at_ns, v.context.event_id),
        )
    )
    if settings.history_window_events is not None:
        rows = rows[-settings.history_window_events :]
    reasons: set[str] = set()
    scope = provider_id
    if settings.provider_pooling_policy is ProviderPooling.EXPLICIT_POOL:
        scope = "EXPLICIT_POOL"
        reasons.add("POOLED_PROVIDER_HISTORY")
    center = scale = None
    if len(rows) < settings.minimum_history:
        reasons.add("INSUFFICIENT_HISTORY")
    else:
        values = [v.raw_surprise for v in rows]
        try:
            center = median(values)
            scale = (
                1.4826 * median(abs(v - center) for v in values)
                if settings.normalization_method is ScaleMethod.MAD
                else stdev(values)
            )
            number(scale)
            if scale <= 0:
                scale = None
                reasons.add("ZERO_SCALE")
        except (ArithmeticError, ValidationError):
            center = scale = None
            reasons.add("NON_FINITE_SCALE")
    return SurpriseScale(
        context,
        cutoff_ns,
        settings.normalization_method,
        scope,
        center,
        scale,
        rows,
        tuple(sorted(reasons)),
        configuration_fingerprint,
    )
