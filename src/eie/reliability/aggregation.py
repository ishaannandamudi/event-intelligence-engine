"""Deterministic untrimmed distributions and strict-prior, tie-safe sample selection."""

from statistics import fmean, median

from eie.core.errors import ValidationError
from eie.reliability.latency import PAIRS, calculate
from eie.reliability.models import LatencyDistribution, ReliabilityQuery, SourceHistoryRow
from eie.reliability.schema import LatencyQuality as Q
from eie.reliability.schema import WindowKind


def select(
    query: ReliabilityQuery, rows: tuple[SourceHistoryRow, ...]
) -> tuple[SourceHistoryRow, ...]:
    eligible = tuple(
        r
        for r in rows
        if r.sample_available_ns < query.known_as_of_ns
        and r.metric_available_ns < query.known_as_of_ns
    )
    if query.policy.window is WindowKind.TRAILING_DAYS:
        assert query.policy.days is not None
        start = max(0, query.known_as_of_ns - query.policy.days * 86_400_000_000_000)
        eligible = tuple(r for r in eligible if r.sample_available_ns >= start)
    if query.policy.window is WindowKind.TRAILING_OBSERVATIONS and eligible:
        assert query.policy.observations is not None
        ordered = sorted(eligible, key=lambda r: r.sample_available_ns, reverse=True)
        boundary = ordered[min(len(ordered), query.policy.observations) - 1].sample_available_ns
        # Include the whole boundary tie; stable IDs never decide metric membership.
        eligible = tuple(r for r in eligible if r.sample_available_ns >= boundary)
    if len(eligible) > query.policy.max_observations:
        raise ValidationError("source observation bound exceeded, including boundary ties")
    return tuple(sorted(eligible, key=lambda r: r.artifact_id))


def quantile(values: list[int], probability: float) -> float:
    position = (len(values) - 1) * probability
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def distributions(
    rows: tuple[SourceHistoryRow, ...], query: ReliabilityQuery
) -> tuple[LatencyDistribution, ...]:
    latency = tuple(calculate(r, query.known_as_of_ns) for r in rows)
    result = []
    for position, (start, end) in enumerate(PAIRS):
        components = [r.components[position] for r in latency]
        values = sorted(
            c.delta
            for c in components
            if c.delta is not None and Q.NEGATIVE_LATENCY not in c.quality
        )
        center = float(median(values)) if values else None
        result.append(
            LatencyDistribution(
                start,
                end,
                len(values),
                sum(Q.MISSING_CLOCK in c.quality for c in components),
                sum(Q.COARSE_TIMESTAMP in c.quality for c in components),
                sum(Q.NEGATIVE_LATENCY in c.quality for c in components),
                center,
                fmean(values) if values else None,
                float(median(abs(v - center) for v in values)) if center is not None else None,
                min(values) if values else None,
                max(values) if values else None,
                tuple((p, quantile(values, p)) for p in query.policy.quantiles) if values else (),
            )
        )
    return tuple(result)
