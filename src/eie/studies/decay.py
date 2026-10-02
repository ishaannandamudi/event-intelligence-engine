"""Observed absolute-return magnitude and nonparametric first sampled peak-fraction crossing."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.studies.aggregation import DistributionQuery, selected
from eie.studies.checks import contract, positive
from eie.studies.models import EventStudyResult, StudyQuality
from eie.studies.numerics import median


@dataclass(frozen=True, slots=True)
class InfluencePoint:
    offset_ns: int
    magnitude: float | None
    sample_count: int

    def __post_init__(self) -> None:
        contract(self)
        positive(self.offset_ns, zero=True)
        positive(self.sample_count, zero=True)
        if self.magnitude is not None and self.magnitude < 0:
            raise ValidationError("negative descriptive influence magnitude")


@dataclass(frozen=True, slots=True)
class EventDecayResult:
    scope: str
    input_fingerprints: tuple[str, ...]
    curve: tuple[InfluencePoint, ...]
    peak_response: float | None
    peak_offset_ns: int | None
    fraction: float
    time_from_peak_ns: int | None
    method: str
    reasons: tuple[str, ...]
    query: DistributionQuery | None = None
    study_quality: frozenset[StudyQuality] = frozenset()

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.scope not in ("SINGLE_EVENT", "EVENT_CLASS")
            or self.method != "FIRST_SAMPLED_FRACTION_OF_PEAK"
            or not 0 < self.fraction < 1
        ):
            raise ValidationError("invalid nonparametric decay contract")
        if self.curve != tuple(sorted(self.curve, key=lambda p: p.offset_ns)) or len(
            {p.offset_ns for p in self.curve}
        ) != len(self.curve):
            raise ValidationError("sorted unique influence offsets required")
        if (self.scope == "EVENT_CLASS") != (self.query is not None):
            raise ValidationError("class decay requires explicit prior query")

    @property
    def half_response_time_ns(self) -> int | None:
        return self.time_from_peak_ns if self.fraction == 0.5 else None

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:event-decay:v1", self)


def summarize(
    curve: tuple[InfluencePoint, ...],
    fingerprints: tuple[str, ...],
    *,
    fraction: float = 0.5,
    query: DistributionQuery | None = None,
    study_quality: frozenset[StudyQuality] = frozenset(),
) -> EventDecayResult:
    if not 0 < fraction < 1:
        raise ValidationError("fraction must be strictly between zero and one")
    values = [p for p in curve if p.magnitude is not None]
    peak = offset = elapsed = None
    reasons = set()
    if any(p.magnitude is None for p in curve):
        reasons.add("INCOMPLETE_CURVE")
    if len(values) < 3:
        reasons.add("INSUFFICIENT_OBSERVATIONS")
    else:
        maximum = max(values, key=lambda p: (p.magnitude or 0, -p.offset_ns))
        peak, offset = maximum.magnitude, maximum.offset_ns
        if peak is None or peak == 0:
            reasons.add("ZERO_RESPONSE")
        else:
            after = [
                p
                for p in values
                if p.offset_ns > offset
                and p.magnitude is not None
                and p.magnitude <= fraction * peak
            ]
            if after:
                elapsed = after[0].offset_ns - offset
            else:
                reasons.add("THRESHOLD_NOT_OBSERVED")
    return EventDecayResult(
        "SINGLE_EVENT" if query is None else "EVENT_CLASS",
        tuple(sorted(fingerprints)),
        curve,
        peak,
        offset,
        fraction,
        elapsed,
        "FIRST_SAMPLED_FRACTION_OF_PEAK",
        tuple(sorted(reasons)),
        query,
        study_quality,
    )


def decay(result: EventStudyResult, *, fraction: float = 0.5) -> EventDecayResult:
    curve = tuple(
        InfluencePoint(
            p.offset_ns, None if p.value is None else abs(p.value), int(p.value is not None)
        )
        for p in result.curve
        if p.offset_ns >= 0
    )
    return summarize(
        curve, (result.result_fingerprint,), fraction=fraction, study_quality=result.quality
    )


def class_decay(
    results: tuple[EventStudyResult, ...], query: DistributionQuery, *, fraction: float = 0.5
) -> EventDecayResult:
    rows = selected(results, query)
    offsets = sorted({p.offset_ns for r in rows for p in r.curve if p.offset_ns >= 0})
    curve = []
    for offset in offsets:
        values = [
            abs(p.value)
            for r in rows
            for p in r.curve
            if p.offset_ns == offset and p.value is not None
        ]
        curve.append(
            InfluencePoint(
                offset,
                median(values) if len(values) >= query.minimum_samples else None,
                len(values),
            )
        )
    return summarize(
        tuple(curve),
        tuple(r.result_fingerprint for r in rows),
        fraction=fraction,
        query=query,
        study_quality=frozenset(q for r in rows for q in r.quality),
    )
