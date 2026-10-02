"""Prior-only transparent matching; deterministic top K without return optimization."""

from math import fsum, hypot, isfinite

from eie.impact.estimation import compatible, fitting_periods
from eie.impact.inputs import ControlCandidate, ImpactInputs
from eie.impact.models import (
    ControlFit,
    CounterfactualModelSpec,
    FitStatus,
    SelectedControl,
    WeightMethod,
)
from eie.studies.market import Adjustment


def select_controls(
    inputs: ImpactInputs, spec: CounterfactualModelSpec
) -> tuple[FitStatus, ControlFit]:
    policy, features, anchor = spec.matching, inputs.target_features, inputs.study.anchor_ns
    assert policy is not None
    cutoff = min(anchor, spec.fit_cutoff_ns)
    matching = max(0, cutoff - 1)
    reasons = []
    eligible: list[ControlCandidate] = []
    if features is None or features.available_ns >= cutoff or features.timestamp_ns >= cutoff:
        return FitStatus.MISSING_INPUT, ControlFit(
            (),
            (),
            (),
            matching,
            ((inputs.target.identity.instrument_id, "MISSING_PRIOR_TARGET_FEATURES"),),
        )
    keys = tuple(k for k, _ in features.values)
    for c in inputs.controls:
        reason = None
        if c.instrument_id == inputs.target.identity.instrument_id:
            reason = "TARGET_EXCLUDED"
        elif c.market is None:
            reason = "MISSING_MARKET"
        elif not compatible(c.market, inputs, spec):
            reason = "INCOMPATIBLE_RETURNS"
        elif (
            c.features is None
            or c.features.available_ns >= cutoff
            or c.features.timestamp_ns >= cutoff
        ):
            reason = "MISSING_PRIOR_FEATURES"
        elif tuple(k for k, _ in c.features.values) != keys:
            reason = "INCOMPATIBLE_FEATURES"
        elif policy.exclude_contaminated and c.contamination:
            reason = "CONTROL_CONTAMINATION"
        elif c.market.identity.adjustment is Adjustment.UNADJUSTED:
            reason = "UNRESOLVED_CORPORATE_ACTION_RISK"
        elif (
            sum(p.value is not None for p in fitting_periods(inputs, c.market, spec))
            < policy.minimum_prior_periods
        ):
            reason = "INSUFFICIENT_PRIOR_MARKET"
        if reason is None:
            eligible.append(c)
        else:
            reasons.append((c.instrument_id, reason))
    if not eligible:
        return FitStatus.INSUFFICIENT, ControlFit((), (), (), matching, tuple(sorted(reasons)))
    try:
        population = [c.features.values for c in eligible if c.features is not None]
        scales = []
        for j, key in enumerate(keys):
            values = tuple(v[j][1] for v in population)
            mean = fsum(v / len(values) for v in values)
            scale = hypot(*(v - mean for v in values)) / len(values) ** 0.5
            if not isfinite(scale):
                raise OverflowError
            scales.append((key, max(policy.minimum_scale, scale)))
        distances = []
        for c in eligible:
            assert c.features is not None
            distance = hypot(
                *(
                    (v - t) / scale
                    for (_, v), (_, t), (_, scale) in zip(
                        c.features.values, features.values, scales, strict=True
                    )
                )
            )
            if not isfinite(distance):
                raise OverflowError
            distances.append((distance, c.instrument_id, c))
        top = sorted(distances, key=lambda v: (v[0], v[1]))[: policy.k]
        if policy.weighting is WeightMethod.EQUAL:
            weights = tuple(1 / len(top) for _ in top)
        else:
            zero_count = sum(d == 0 for d, _, _ in top)
            if zero_count:
                weights = tuple(float(d == 0) / zero_count for d, _, _ in top)
            else:
                # Scale by smallest distance to avoid overflowing 1/distance.
                raw = tuple(top[0][0] / d for d, _, _ in top)
                total = fsum(raw)
                weights = tuple(v / total for v in raw)
        selected = tuple(
            SelectedControl(c.instrument_id, d, weight, c.features, bool(c.contamination))
            for (d, _, c), weight in zip(top, weights, strict=True)
            if c.features is not None
        )
        return FitStatus.AVAILABLE, ControlFit(
            selected,
            tuple(scales),
            tuple(sorted(c.instrument_id for c in eligible)),
            matching,
            tuple(sorted(reasons)),
        )
    except ArithmeticError:
        return FitStatus.NUMERIC_UNAVAILABLE, ControlFit(
            (),
            (),
            (),
            matching,
            tuple(sorted((*reasons, (features.instrument_id, "NUMERIC_UNAVAILABLE")))),
        )
