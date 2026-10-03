"""Compatible prior-only numeric comparisons with explicit unavailable arithmetic."""

from math import isfinite
from statistics import median

from eie.corporate.types import EarningsPeriod
from eie.evidence.claims import NumericRange
from eie.evidence.resolution import compatible
from eie.expectations.units import CanonicalUnit, Quantity
from eie.intelligence.inputs import FactInput
from eie.intelligence.models import Metric, NoveltyComponent
from eie.intelligence.policy import NoveltyPolicy
from eie.intelligence.schema import NoveltyDimension, NoveltyState


def metric(
    name: str, value: float | None, unit: CanonicalUnit | None = None, reason: str = "MISSING_INPUT"
) -> Metric:
    if value is not None and not isfinite(value):
        return Metric(name, None, unit, "NUMERIC_OVERFLOW")
    return Metric(name, value, unit, reason if value is None else None)


def bounds(value: Quantity | NumericRange) -> tuple[float | None, float | None, float | None]:
    if isinstance(value, Quantity):
        point = float(value.canonical_value)
        return point, point, point
    return (
        None if value.lower is None else float(value.lower.canonical_value),
        None if value.upper is None else float(value.upper.canonical_value),
        None if value.point is None else float(value.point.canonical_value),
    )


def delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def period_key(fact: FactInput) -> tuple[str, str, str]:
    context = fact.claim.measurement
    assert context is not None
    period = context.reference_period
    if isinstance(period, EarningsPeriod):
        return (
            period.period_end,
            period.period_end,
            "ANNUAL" if period.fiscal_quarter is None else "QUARTERLY",
        )
    return period.start_date, period.end_date, period.frequency.value


def numeric_cohort(fact: FactInput) -> object:
    c = fact.claim
    context = c.measurement
    assert context is not None
    return (
        c.claim_type,
        c.subject.kind,
        c.subject.identity
        if not c.subject.identity.startswith("evt_")
        else (None if fact.event is None else fact.event.identity.scope),
        None if fact.event is None else fact.event.identity.event_type,
        context.measurement_id,
        c.unit,
        context.release_stage,
        context.seasonal_adjustment,
        c.measurement_semantics,
        type(context.reference_period).__name__,
        period_key(fact)[2],
    )


def numeric_novelty(
    current: FactInput,
    prior: tuple[FactInput, ...],
    excluded: tuple[str, ...],
    policy: NoveltyPolicy,
) -> NoveltyComponent:
    value = current.assertion.value
    assert isinstance(value, (Quantity, NumericRange))
    cohort = tuple(
        p
        for p in prior
        if isinstance(p.assertion.value, (Quantity, NumericRange))
        and p.assertion.event_id not in excluded
        and p.resolution.selected_id == p.assertion.observation_id
        and numeric_cohort(p) == numeric_cohort(current)
        and period_key(p)[1] < period_key(current)[0]
    )
    # A reporting period is one observation, even when several feeds repeat it.
    # Competing unresolved period values are excluded, never averaged or silently merged.
    periods: dict[tuple[str, str, str], list[FactInput]] = {}
    for p in cohort:
        periods.setdefault(period_key(p), []).append(p)
    unique: list[FactInput] = []
    ambiguous = False
    for key in sorted(periods):
        candidates = periods[key]
        if any(
            not compatible(candidates[0].assertion.value, p.assertion.value) for p in candidates[1:]
        ):
            ambiguous = True
            continue
        order = policy.resolution.precedence.order_for(current.claim.claim_type.value)
        unique.append(
            min(
                candidates,
                key=lambda p: (
                    order.index(p.assertion.source.source_class),
                    -p.assertion.known_from_ns,
                    p.assertion.observation_id,
                ),
            )
        )
    samples = tuple(unique[-policy.historical_samples :])
    low, high, point = bounds(value)
    last = None if not samples else samples[-1].assertion.value
    assert last is None or isinstance(last, (Quantity, NumericRange))
    plow, phigh, previous = (None, None, None) if last is None else bounds(last)
    points = [
        bounds(p.assertion.value)[2]
        for p in samples
        if isinstance(p.assertion.value, (Quantity, NumericRange))
    ]
    numbers = [p for p in points if p is not None]
    center = float(median(numbers)) if numbers else None
    mad = None if center is None else float(median(abs(p - center) for p in numbers))
    relative = (
        None
        if point is None or previous is None or abs(previous) <= policy.near_zero
        else (point - previous) / abs(previous)
    )
    standardized = (
        None
        if point is None
        or center is None
        or mad is None
        or not isfinite(mad)
        or not isfinite(center)
        or mad == 0
        or len(numbers) < policy.minimum_numeric_samples
        else (point - center) / mad
    )
    reason = "NO_COMPARABLE_PRIOR" if not samples else "RANGE_WITHOUT_POINT"
    relative_reason = (
        "NEAR_ZERO_BASELINE"
        if previous is not None and abs(previous) <= policy.near_zero
        else reason
    )
    scale_reason = (
        "INSUFFICIENT_SAMPLES"
        if len(numbers) < policy.minimum_numeric_samples
        else "ZERO_MAD"
        if mad == 0
        else "NUMERIC_OVERFLOW"
        if mad is not None and not isfinite(mad)
        else reason
    )
    metrics = (
        metric("absolute_change", delta(point, previous), value.canonical_unit, reason),
        metric("high_displacement", delta(high, phigh), value.canonical_unit, reason),
        metric("low_displacement", delta(low, plow), value.canonical_unit, reason),
        metric("median", center, value.canonical_unit, "NO_POINT_BASELINE"),
        metric("median_absolute_deviation", mad, value.canonical_unit, "NO_POINT_BASELINE"),
        metric("prior_sample_count", float(len(samples))),
        metric("relative_change", relative, reason=relative_reason),
        metric("standardized_mad", standardized, reason=scale_reason),
    )
    reasons = tuple(
        sorted(
            {m.reason for m in metrics if m.reason is not None}
            | ({"HISTORICAL_SAMPLE_WINDOW"} if len(unique) > len(samples) else set())
            | ({"AMBIGUOUS_PERIOD_BASELINE_EXCLUDED"} if ambiguous else set())
        )
    )
    return NoveltyComponent(
        NoveltyDimension.NUMERIC,
        current.claim.claim_id,
        NoveltyState.COMPARABLE if samples else NoveltyState.NO_BASELINE,
        (current.assertion.observation_id,),
        tuple(sorted(p.assertion.observation_id for p in samples)),
        metrics,
        reasons,
    )
