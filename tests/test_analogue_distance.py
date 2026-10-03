from dataclasses import replace

import pytest
from analogue_helpers import NUMERIC, POLICY, TYPE, inputs, response

from eie.analogues.distance import distance, scales
from eie.analogues.models import AnalogueFeature, AnaloguePolicy
from eie.analogues.retrieval import retrieve
from eie.analogues.schema import (
    Exclusion,
    FeatureKind,
    FeatureSource,
    MissingPolicy,
    ScaleMethod,
    SectorCondition,
    ZeroScalePolicy,
)


@pytest.mark.parametrize("method,expected", [(ScaleMethod.MAD, 2.0), (ScaleMethod.IQR, 3.0)])
def test_robust_scales_exclude_target_and_pin_members(method, expected):
    p = replace(POLICY, features=(replace(NUMERIC, scale=method), TYPE))
    data, q = inputs(target_value=100000, policy=p)
    s = scales(data.target, data.rows, p)[0]
    assert s.count == 7 and s.value == expected
    assert scales(
        replace(
            data.target,
            values=(replace(data.target.values[0], value=-90000), data.target.values[1]),
        ),
        data.rows,
        p,
    ) == (s,)
    result = retrieve(data, q, p)
    assert result.diagnostics.nearest_distance > p.maximum_distance
    assert not result.candidates and result.state.value == "UNAVAILABLE"


def test_weighted_normalization_components_are_visible():
    p = replace(POLICY, features=(replace(NUMERIC, weight=3), replace(TYPE, weight=1)))
    data, q = inputs(target_value=5, policy=p)
    s = scales(data.target, data.rows, p)
    r = data.rows[0]
    candidate, exclusion = distance(data.target, r, response(r), p, s)
    assert exclusion is None
    assert candidate.total_weight == candidate.comparable_weight == 4
    assert candidate.numerator == 6 and candidate.total_distance == 1.5
    assert candidate.components[0].target == 5 and candidate.components[0].candidate == 1
    assert candidate.components[0].scale == 2


@pytest.mark.parametrize("missing", list(MissingPolicy))
@pytest.mark.parametrize("which", ["target", "candidate", "signature", "scale"])
def test_missingness_is_explicit_and_never_zero(missing, which):
    p = replace(
        POLICY,
        features=(replace(NUMERIC, missing=missing, missing_penalty=0.4, weight=3), TYPE),
        minimum_weight_fraction=0.25,
    )
    data, _ = inputs(policy=p)
    target, candidate = data.target, data.rows[0]
    if which == "target":
        target = replace(
            target,
            values=(replace(target.values[0], value=None, reason="MISSING"), target.values[1]),
        )
    elif which == "candidate":
        candidate = replace(
            candidate,
            values=(
                replace(candidate.values[0], value=None, reason="MISSING"),
                candidate.values[1],
            ),
        )
    elif which == "signature":
        candidate = replace(
            candidate,
            values=(replace(candidate.values[0], signature="OTHER_UNIT"), candidate.values[1]),
        )
    history = data.rows[:2] if which == "scale" else data.rows
    result, exclusion = distance(
        target, candidate, response(candidate), p, scales(target, history, p)
    )
    if missing is MissingPolicy.IGNORE_WITH_PENALTY:
        assert exclusion is None and result.comparable_weight == 1
        assert result.missing_penalty == pytest.approx(1.2)
        assert result.total_distance == pytest.approx(0.3)
        assert result.components[0].distance is None and result.components[0].reason
    else:
        assert result is None and exclusion is Exclusion.MISSING_REQUIRED_FEATURE


@pytest.mark.parametrize("zero", list(ZeroScalePolicy))
@pytest.mark.parametrize("target_value", [1.0, 2.0])
def test_zero_scale_has_declared_semantics(zero, target_value):
    p = replace(POLICY, features=(replace(NUMERIC, zero_scale=zero), TYPE))
    data, q = inputs(policy=p, values=(1.0,) * 7, target_value=target_value)
    result = retrieve(data, q, p)
    assert result.scales[0].value == 0
    if zero is ZeroScalePolicy.UNAVAILABLE:
        assert not result.candidates
    else:
        assert result.candidates[0].components[0].rule == "ZERO_SCALE_EXACT_MATCH"
        assert result.candidates[0].total_distance == (0 if target_value == 1 else 0.5)


@pytest.mark.parametrize("count,fraction", [(2, 0.5), (1, 0.6)])
def test_minimum_comparability_rejects_sparse_vectors(count, fraction):
    p = replace(
        POLICY,
        features=(replace(NUMERIC, missing=MissingPolicy.IGNORE_WITH_PENALTY), TYPE),
        minimum_comparable_features=count,
        minimum_weight_fraction=fraction,
    )
    data, q = inputs(policy=p, values=(None,) * 7)
    result = retrieve(data, q, p)
    assert not result.candidates
    assert dict(result.diagnostics.excluded)[Exclusion.INSUFFICIENT_COMPARABLE_FEATURES] == 7


@pytest.mark.parametrize(
    "kind,a,b,expected",
    [
        (FeatureKind.CATEGORICAL, "BMO", "BMO", 0),
        (FeatureKind.CATEGORICAL, "BMO", "AMC", 1),
        (FeatureKind.BOOLEAN, True, False, 1),
        (FeatureKind.ORDINAL, "LOW", "HIGH", 1),
        (FeatureKind.ORDINAL, "LOW", "MID", 0.5),
    ],
)
def test_named_categorical_boolean_and_ordinal_comparison(kind, a, b, expected):
    f = AnalogueFeature(
        "number",
        kind,
        FeatureSource.CONTEXT,
        "regime",
        ordinal_labels=("LOW", "MID", "HIGH") if kind is FeatureKind.ORDINAL else (),
    )
    p = AnaloguePolicy(features=(f,), sector_condition=SectorCondition.UNCONDITIONED)
    data, _ = inputs(policy=p, target_value=a, values=(b,) * 7)
    result, reason = distance(data.target, data.rows[0], response(data.rows[0]), p, ())
    assert reason is None and result.total_distance == expected


def test_ties_sort_by_stable_event_revision_and_survive_insertion_order():
    data, q = inputs(values=(1.0,) * 7, target_value=1)
    a = retrieve(data, q, POLICY)
    b = retrieve(
        replace(data, rows=tuple(reversed(data.rows)), responses=tuple(reversed(data.responses))),
        q,
        POLICY,
    )
    # Pure inputs can be unordered; public pool collection canonicalizes them.
    assert a.candidates == b.candidates
    assert tuple(c.event_id for c in a.candidates) == tuple(
        sorted(c.event_id for c in a.candidates)
    )


def test_top_k_and_distance_threshold_no_forced_matches():
    data, q = inputs()
    result = retrieve(data, replace(q, top_k=2), POLICY)
    assert len(result.candidates) == 2 and result.state.value == "INSUFFICIENT_ANALOGUES"
    p = replace(POLICY, maximum_distance=0)
    data, q = inputs(policy=p)
    result = retrieve(data, q, p)
    assert len(result.candidates) == 1 and result.candidates[0].total_distance == 0


def test_finite_extreme_values_fail_closed_without_epsilon():
    data, q = inputs(values=(-1.7e308, -1.7e308, 0, 1.7e308, 1.7e308, 0, 0), target_value=1.7e308)
    result = retrieve(data, q, POLICY)
    assert result.scales[0].value == 1.7e308
    assert all(c.total_distance < float("inf") for c in result.candidates)
    data, q = inputs(values=(-1.7e308,) * 6 + (1.7e308,), target_value=1.7e308)
    # Unrepresentable deviations remain unavailable even when their median would be zero.
    assert retrieve(data, q, POLICY).scales[0].reason == "NUMERIC_OVERFLOW"
