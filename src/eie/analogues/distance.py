"""Prior robust scaling and explicit weighted distances; no learned neighbors."""

from math import fsum, isfinite

from eie.analogues.models import (
    AnalogueCandidate,
    AnaloguePolicy,
    DistanceComponent,
    FeatureRow,
    NumericScale,
    ResponseObservation,
)
from eie.analogues.schema import Exclusion, FeatureKind, MissingPolicy, ScaleMethod, ZeroScalePolicy
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.studies.aggregation import quantile
from eie.studies.numerics import median


def scales(
    target: FeatureRow,
    rows: tuple[FeatureRow, ...],
    policy: AnaloguePolicy,
) -> tuple[NumericScale, ...]:
    targets = {v.name: v for v in target.values}
    result = []
    for feature in policy.features:
        if feature.kind is not FeatureKind.NUMERIC:
            continue
        members = tuple(
            sorted(
                (r.row_id, float(v.value))
                for r in rows
                for v in r.values
                if v.name == feature.name
                and v.value is not None
                and v.signature == targets[feature.name].signature
                and isinstance(v.value, (int, float))
                and not isinstance(v.value, bool)
            )
        )
        value = None
        reason = "INSUFFICIENT_PRIOR_SCALE"
        if len(members) >= policy.minimum_scale_samples:
            values = tuple(v for _, v in members)
            center = median(values)
            try:
                value = (
                    median(abs(v - center) for v in values)
                    if feature.scale is ScaleMethod.MAD
                    else quantile(values, 0.75) - quantile(values, 0.25)
                )
                if not isfinite(value):
                    raise OverflowError
            except (OverflowError, ValidationError):
                value, reason = None, "NUMERIC_OVERFLOW"
        result.append(
            NumericScale(
                feature.name,
                feature.scale,
                len(members),
                value,
                reason if value is None else None,
                fingerprint("eie:analogue-scale-members:v1", members),
            )
        )
    return tuple(result)


def distance(
    target: FeatureRow,
    candidate: FeatureRow,
    response: ResponseObservation,
    policy: AnaloguePolicy,
    prior_scales: tuple[NumericScale, ...],
) -> tuple[AnalogueCandidate | None, Exclusion | None]:
    target_values, candidate_values = ({v.name: v for v in r.values} for r in (target, candidate))
    scale_map = {s.feature: s for s in prior_scales}
    components = []
    comparable = []
    for feature in policy.features:
        a, b = target_values[feature.name], candidate_values[feature.name]
        scale = None
        d = None
        reason = a.reason or b.reason
        rule = "MISSING"
        if a.signature != b.signature:
            reason = "INCOMPATIBLE_FEATURE_SEMANTICS"
        elif a.value is not None and b.value is not None:
            if feature.kind is FeatureKind.NUMERIC:
                s = scale_map[feature.name]
                scale = s.value
                if scale is None:
                    reason = s.reason
                elif scale == 0:
                    if feature.zero_scale is ZeroScalePolicy.EXACT_MATCH:
                        d, rule = float(a.value != b.value), "ZERO_SCALE_EXACT_MATCH"
                    else:
                        reason = "ZERO_SCALE_UNAVAILABLE"
                else:
                    assert isinstance(a.value, (int, float)) and isinstance(b.value, (int, float))
                    d, rule = (
                        abs(float(a.value) - float(b.value)) / scale,
                        "ABS_DIFFERENCE/PRIOR_" + feature.scale.value,
                    )
                    if not isfinite(d):
                        d, reason = None, "NUMERIC_OVERFLOW"
            elif feature.kind is FeatureKind.ORDINAL:
                assert isinstance(a.value, str) and isinstance(b.value, str)
                d = abs(
                    feature.ordinal_labels.index(a.value) - feature.ordinal_labels.index(b.value)
                ) / (len(feature.ordinal_labels) - 1)
                rule = "ORDINAL_INDEX_GAP/(LEVELS-1)"
            else:
                d, rule = float(a.value != b.value), "EXACT_MATCH_0_MISMATCH_1"
        penalty = 0.0
        if d is None:
            if feature.missing is not MissingPolicy.IGNORE_WITH_PENALTY:
                return None, Exclusion.MISSING_REQUIRED_FEATURE
            penalty = feature.weight * feature.missing_penalty
        else:
            comparable.append(feature.weight)
            reason = None
        components.append(
            DistanceComponent(
                feature.name,
                feature.kind,
                a.value,
                b.value,
                feature.weight,
                d,
                scale,
                penalty,
                rule,
                reason,
            )
        )
    total_weight, comparable_weight = fsum(f.weight for f in policy.features), fsum(comparable)
    if (
        len(comparable) < policy.minimum_comparable_features
        or comparable_weight / total_weight < policy.minimum_weight_fraction
    ):
        return None, Exclusion.INSUFFICIENT_COMPARABLE_FEATURES
    try:
        numerator = fsum(c.weight * c.distance for c in components if c.distance is not None)
        missing = fsum(c.penalty for c in components)
        total = (numerator + missing) / total_weight
        if not isfinite(total):
            raise OverflowError
    except OverflowError:
        return None, Exclusion.INSUFFICIENT_COMPARABLE_FEATURES
    assert response.value is not None
    result = AnalogueCandidate(
        candidate.row_id,
        candidate.event_id,
        candidate.revision,
        candidate.event_available_ns,
        response.response_id,
        response.available_ns,
        candidate.values,
        tuple(components),
        total,
        numerator,
        comparable_weight,
        total_weight,
        missing,
        len(comparable),
        response.value,
        response.contaminated,
        tuple(sorted(set(candidate.quality) | set(response.quality))),
    )
    return result, Exclusion.MAXIMUM_DISTANCE if total > policy.maximum_distance else None
