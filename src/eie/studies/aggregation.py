"""Strict-prior descriptive distributions, compatible cohorts and explicit conditioning."""

from dataclasses import dataclass
from math import fsum, isfinite

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.studies.checks import contract, digest, positive
from eie.studies.models import EventStudyResult, StudyQuality
from eie.studies.numerics import median


@dataclass(frozen=True, slots=True)
class DistributionQuery:
    as_of_ns: int
    event_build_id: str
    event_type_code: str
    cohort_fingerprint: str
    offset_ns: int
    minimum_samples: int
    exclude_event_id: str | None = None
    issuer_id: str | None = None
    context: tuple[tuple[str, str], ...] = ()
    surprise_boundaries: tuple[float, ...] = ()
    surprise_bucket: int | None = None
    measurement: str | None = None
    exclude_contaminated: bool = False

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.as_of_ns)
        digest(self.cohort_fingerprint)
        positive(self.minimum_samples)
        if self.context != tuple(sorted(set(self.context))) or len(dict(self.context)) != len(
            self.context
        ):
            raise ValidationError("sorted unique conditioning dimensions required")
        if self.surprise_boundaries != tuple(sorted(set(self.surprise_boundaries))) or (
            self.surprise_bucket is not None
            and not 0 <= self.surprise_bucket <= len(self.surprise_boundaries)
        ):
            raise ValidationError("invalid explicit surprise bucket boundaries")
        if self.surprise_bucket is not None and self.measurement is None:
            raise ValidationError("surprise conditioning requires measurement identity")


@dataclass(frozen=True, slots=True)
class ResponseDistribution:
    query: DistributionQuery
    study_ids: tuple[str, ...]
    raw_values: tuple[float, ...]
    mean: float | None
    median: float | None
    quantiles: tuple[tuple[float, float], ...]
    mad: float | None
    median_absolute_response: float | None
    positive_frequency: float | None
    negative_frequency: float | None
    median_volatility_change: float | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        if len(self.study_ids) != len(self.raw_values) or self.study_ids != tuple(
            sorted(set(self.study_ids))
        ):
            raise ValidationError("distribution samples must be sorted unique")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValidationError("sorted distribution reasons required")

    @property
    def count(self) -> int:
        return len(self.raw_values)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:response-distribution:v1", self)


def cohort(result: EventStudyResult) -> str:
    return fingerprint(
        "eie:study-cohort:v1",
        (
            result.definition.event_build_id,
            result.definition.event_dataset_fingerprint,
            result.definition.window,
            result.definition.policy,
            result.market.identity.category,
            result.market.identity.timeframe,
            result.market.identity.adjustment,
            result.market.identity.return_type,
            result.definition.engine_version,
            result.definition.schema_version,
        ),
    )


def quantile(values: tuple[float, ...], probability: float) -> float:
    if (
        not values
        or any(not isfinite(v) for v in values)
        or not isfinite(probability)
        or not 0 <= probability <= 1
    ):
        raise ValidationError("quantile requires samples and probability in [0,1]")
    ordered = sorted(values)
    rank = (len(values) - 1) * probability
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def selected(
    results: tuple[EventStudyResult, ...], query: DistributionQuery
) -> tuple[EventStudyResult, ...]:
    rows = []
    for r in sorted(results, key=lambda r: r.study_id):
        d = r.definition
        if (
            r.available_ns >= query.as_of_ns
            or r.anchor_ns >= query.as_of_ns
            or d.event.revision.available_from_ns >= query.as_of_ns
            or d.event.event_id == query.exclude_event_id
        ):
            continue
        if (
            d.event_build_id != query.event_build_id
            or d.event.identity.event_type.code != query.event_type_code
        ):
            continue
        if (
            query.issuer_id is not None
            and query.issuer_id not in d.event.identity.scope.identifiers
        ):
            continue
        if query.exclude_contaminated and r.contamination:
            continue
        if any((k, v) not in {(c.dimension, c.label) for c in d.context} for k, v in query.context):
            continue
        if query.surprise_bucket is not None:
            s = d.surprise
            if (
                s is None
                or s.normalized_surprise is None
                or s.context.measurement_id.code != query.measurement
            ):
                continue
            if (
                sum(s.normalized_surprise >= boundary for boundary in query.surprise_boundaries)
                != query.surprise_bucket
            ):
                continue
        if cohort(r) != query.cohort_fingerprint:
            raise ValidationError(
                "incompatible return/window/policy/build cohort; filter explicitly"
            )
        rows.append(r)
    if len({r.definition.event.event_id for r in rows}) != len(rows):
        raise ValidationError(
            "multiple revisions/instruments/windows per event require explicit cohort selection"
        )
    return tuple(rows)


def historical_distribution(
    results: tuple[EventStudyResult, ...], query: DistributionQuery
) -> ResponseDistribution:
    rows = []
    reasons = set()
    for r in selected(results, query):
        values = [p.value for p in r.curve if p.offset_ns == query.offset_ns]
        if not values or values[0] is None:
            reasons.add("MISSING_RESPONSE_SAMPLES")
            continue
        rows.append((r, values[0]))
        if r.quality:
            reasons.add("STUDIES_WITH_QUALITY_WARNINGS")
        if StudyQuality.EVENT_HISTORY_INCOMPLETE in r.quality:
            reasons.add("EVENT_HISTORY_INCOMPLETE")
    samples = tuple(v for _, v in rows)
    ids = tuple(r.study_id for r, _ in rows)
    center = avg = mad = absolute = pos = neg = vol = None
    quantiles: tuple[tuple[float, float], ...] = ()
    if len(samples) < query.minimum_samples:
        reasons.add("INSUFFICIENT_SAMPLES")
    else:
        center = median(samples)
        try:
            avg = fsum(v / len(samples) for v in samples)
            mad = median(abs(v - center) for v in samples)
            if not isfinite(avg) or not isfinite(mad):
                raise OverflowError
            absolute = median(abs(v) for v in samples)
            pos = sum(v > 0 for v in samples) / len(samples)
            neg = sum(v < 0 for v in samples) / len(samples)
            quantiles = tuple((p, quantile(samples, p)) for p in (0.1, 0.25, 0.5, 0.75, 0.9))
            vol_samples = [
                r.volatility.difference for r, _ in rows if r.volatility.difference is not None
            ]
            vol = median(vol_samples) if len(vol_samples) >= query.minimum_samples else None
        except OverflowError:
            avg = center = mad = absolute = pos = neg = vol = None
            quantiles = ()
            reasons.add("NUMERIC_OVERFLOW")
    return ResponseDistribution(
        query,
        ids,
        samples,
        avg,
        center,
        quantiles,
        mad,
        absolute,
        pos,
        neg,
        vol,
        tuple(sorted(reasons)),
    )


def validate_distribution(
    distribution: ResponseDistribution, results: tuple[EventStudyResult, ...]
) -> bool:
    """Verify sample IDs/statistics and strict-prior selection against supplied history."""
    return historical_distribution(results, distribution.query) == distribution
