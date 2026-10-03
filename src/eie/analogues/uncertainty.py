"""Eight independent diagnostics; no aggregate probability or confidence score."""

from math import isfinite

from eie.analogues.models import (
    AnalogueCandidate,
    AnaloguePolicy,
    AnalogueUncertainty,
    PoolDiagnostics,
    ReactionDistribution,
)
from eie.analogues.schema import UncertaintyKind as K
from eie.analogues.schema import UncertaintyState as S


def decompose(
    diagnostics: PoolDiagnostics,
    candidates: tuple[AnalogueCandidate, ...],
    distribution: ReactionDistribution,
    policy: AnaloguePolicy,
    reconstruction_count: int,
    contaminated_count: int,
) -> tuple[AnalogueUncertainty, ...]:
    d = diagnostics
    coverage = (
        sum(c.comparable_weight / c.total_weight for c in candidates) / len(candidates)
        if candidates
        else None
    )
    sparse_context = any(found < total for _, found, total in d.context_coverage)
    stability_values = tuple(v for _, v in d.stability_medians)
    stability = max(stability_values) - min(stability_values) if len(stability_values) > 1 else None
    overflow = stability is not None and not isfinite(stability)
    if overflow:
        stability = None
    components = (
        AnalogueUncertainty(
            K.POOL_SIZE,
            S.OBSERVED if d.selected >= policy.minimum_history else S.LIMITED,
            (
                "SUFFICIENT_SELECTED_HISTORY"
                if d.selected >= policy.minimum_history
                else "SMALL_ANALOGUE_POOL",
            ),
            (("eligible", float(d.eligible)), ("selected", float(d.selected))),
        ),
        AnalogueUncertainty(
            K.FEATURE_COVERAGE,
            S.UNAVAILABLE if coverage is None else S.LIMITED if coverage < 1 else S.OBSERVED,
            (
                "NO_COMPARABLE_FEATURES"
                if coverage is None
                else "MISSING_COMPARISON_DIMENSIONS"
                if coverage < 1
                else "ALL_DIMENSIONS_COMPARABLE",
            ),
            (("comparable_weight_fraction", coverage),),
        ),
        AnalogueUncertainty(
            K.DISTANCE_QUALITY,
            S.UNAVAILABLE
            if d.nearest_distance is None
            else S.LIMITED
            if d.nearest_distance > policy.poor_distance_threshold
            else S.OBSERVED,
            (
                "NO_MEASURED_DISTANCE"
                if d.nearest_distance is None
                else "POOR_NEAREST_DISTANCE"
                if d.nearest_distance > policy.poor_distance_threshold
                else "WITHIN_DECLARED_DISTANCE_THRESHOLD",
            ),
            (
                ("median_selected_distance", d.median_distance),
                ("nearest_distance", d.nearest_distance),
            ),
        ),
        AnalogueUncertainty(
            K.RESPONSE_COVERAGE,
            S.LIMITED if d.response_complete < d.blocked_candidates else S.OBSERVED,
            (
                "INCOMPLETE_OR_EXCLUDED_RESPONSES"
                if d.response_complete < d.blocked_candidates
                else "COMPLETE_RESPONSES",
            ),
            (
                ("blocked", float(d.blocked_candidates)),
                ("response_complete", float(d.response_complete)),
            ),
        ),
        AnalogueUncertainty(
            K.CONTEXT_COVERAGE,
            S.UNAVAILABLE
            if not d.context_coverage
            else S.LIMITED
            if sparse_context
            else S.OBSERVED,
            (
                "NO_CONTEXT_REQUESTED"
                if not d.context_coverage
                else "SPARSE_HISTORICAL_CONTEXT"
                if sparse_context
                else "REQUESTED_CONTEXT_PRESENT",
            ),
        ),
        AnalogueUncertainty(
            K.CONTAMINATION,
            S.LIMITED if contaminated_count else S.OBSERVED,
            ("CONTAMINATED_HISTORY" if contaminated_count else "NO_RETAINED_CONTAMINATION_FLAGS",),
            (("flagged_count", float(contaminated_count)),),
        ),
        AnalogueUncertainty(
            K.RECONSTRUCTION,
            S.LIMITED if reconstruction_count else S.OBSERVED,
            (
                "QUALIFIED_HISTORICAL_RECONSTRUCTION"
                if reconstruction_count
                else "NO_RECONSTRUCTION_QUALIFICATION",
            ),
            (("qualified_count", float(reconstruction_count)),),
        ),
        AnalogueUncertainty(
            K.DISTRIBUTION_STABILITY,
            S.UNAVAILABLE
            if stability is None
            else S.LIMITED
            if stability > policy.stability_threshold
            else S.OBSERVED,
            (
                "NESTED_MEDIAN_NUMERIC_OVERFLOW"
                if overflow
                else "INSUFFICIENT_NESTED_SUBSETS"
                if stability is None
                else "NESTED_MEDIAN_INSTABILITY"
                if stability > policy.stability_threshold
                else "NESTED_MEDIANS_WITHIN_THRESHOLD",
            ),
            (("median_range", stability), ("response_mad", distribution.mad)),
        ),
    )
    return tuple(sorted(components, key=lambda u: u.kind))
