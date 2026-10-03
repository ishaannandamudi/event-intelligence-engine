"""Ex-post midrank percentiles and residuals against an unchanged frozen analogue set."""

from math import isfinite

from eie.analogues.models import AnalogueResult, MarketReactionSurprise, ResponseObservation
from eie.analogues.schema import ResultState
from eie.core.errors import ValidationError


def percentile(values: tuple[float, ...], target: float) -> float:
    if not values or not isfinite(target) or any(not isfinite(v) for v in values):
        raise ValidationError("finite nonempty empirical percentile required")
    return (sum(v < target for v in values) + 0.5 * sum(v == target for v in values)) / len(values)


def compare(
    frozen: AnalogueResult,
    target: ResponseObservation | None,
    available_ns: int,
) -> MarketReactionSurprise:
    q, d = frozen.query, frozen.distribution
    if available_ns < q.available_ns:
        raise ValidationError("reaction comparison precedes frozen result availability")
    signed = magnitude = residual = standardized = None
    reasons: set[str] = set(d.reasons)
    state = ResultState.UNAVAILABLE
    if target is None:
        reasons.add("TARGET_RESPONSE_NOT_AVAILABLE")
    else:
        if (target.feature_row_id, target.event_id, target.revision, target.spec) != (
            q.target_row_id,
            q.target_event_id,
            q.target_revision,
            q.response,
        ) or available_ns <= max(target.completed_ns, target.available_ns):
            raise ValidationError("ex-post target must pin exact row/cohort and completed response")
        reasons.update(target.quality)
        if target.value is None:
            reasons.add(target.reason or "TARGET_METRIC_NOT_AVAILABLE")
        elif d.median is None:
            reasons.add("ANALOGUE_DISTRIBUTION_UNAVAILABLE")
            state = ResultState.INSUFFICIENT_ANALOGUES
        else:
            signed = percentile(d.raw_values, target.value)
            magnitude = percentile(tuple(abs(v) for v in d.raw_values), abs(target.value))
            residual = target.value - d.median
            if not isfinite(residual):
                residual = None
                reasons.add("RESIDUAL_NUMERIC_OVERFLOW")
            if d.mad is None:
                reasons.add("MAD_NOT_AVAILABLE")
            elif d.mad == 0:
                reasons.add("ZERO_MAD")
            elif residual is not None:
                standardized = residual / d.mad
                if not isfinite(standardized):
                    standardized = None
                    reasons.add("STANDARDIZED_RESIDUAL_NUMERIC_OVERFLOW")
            state = ResultState.AVAILABLE
    return MarketReactionSurprise(
        frozen.result_id,
        frozen.fingerprint,
        None if target is None else target.response_id,
        None if target is None else target.fingerprint,
        None if target is None else target.value,
        available_ns,
        signed,
        magnitude,
        residual,
        standardized,
        state,
        tuple(sorted(reasons)),
    )
