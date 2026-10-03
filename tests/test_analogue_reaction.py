from dataclasses import replace

import pytest
from analogue_helpers import POLICY, SPEC, inputs, response

from eie.analogues.reaction_surprise import compare, percentile
from eie.analogues.responses import distribution
from eie.analogues.retrieval import retrieve
from eie.analogues.schema import ResultState


@pytest.mark.parametrize(
    "values,target,expected",
    [
        ((-2.0, -1.0, 0.0, 1.0, 2.0), 0, 0.5),
        ((1.0, 1.0, 1.0), 1, 0.5),
        ((1.0, 1.0, 2.0), 1, 1 / 3),
        ((1.0, 2.0), 0, 0),
        ((1.0, 2.0), 3, 1),
        ((1.0, 2.0), 1.5, 0.5),
    ],
)
def test_empirical_midrank_ties(values, target, expected):
    assert percentile(values, target) == pytest.approx(expected)


@pytest.mark.parametrize("values,target", [((), 1), ((float("nan"),), 1), ((1.0,), float("inf"))])
def test_percentile_rejects_invalid_samples(values, target):
    with pytest.raises(ValueError):
        percentile(values, target)


def test_type7_distribution_preserves_unweighted_raw_outliers():
    d = distribution(SPEC, tuple((str(i), float(v)) for i, v in enumerate((0, 1, 2, 3, 100))), 5)
    assert d.count == 5 and d.raw_values[-1] == 100
    assert d.mean == 21.2 and d.median == 2 and d.mad == 1
    assert dict(d.quantiles)[0.9] == pytest.approx(61.2)
    assert d.positive_frequency == 0.8 and d.negative_frequency == 0
    assert d.median_absolute == 2 and d.minimum == 0 and d.maximum == 100


@pytest.mark.parametrize("count", [0, 1, 4, 5])
def test_minimum_count_has_typed_missing_statistics(count):
    d = distribution(SPEC, tuple((str(i), float(i)) for i in range(count)), 5)
    assert (d.median is None) == (count < 5)
    assert bool(d.reasons) == (count < 5)
    assert d.count == count


def test_signed_and_magnitude_surprise_are_separate():
    data, q = inputs(count=5, response_values=(-4, -2, 0, 2, 4))
    frozen = retrieve(data, q, POLICY)
    r = response(data.target, -3)
    surprise = compare(frozen, r, r.available_ns + 1)
    assert surprise.state is ResultState.AVAILABLE
    assert surprise.signed_percentile == 0.2 and surprise.magnitude_percentile == 0.6
    assert surprise.signed_residual == -3 and surprise.standardized_mad_residual == -1.5
    assert surprise.analogue_fingerprint == frozen.fingerprint
    assert surprise.target_response_id == r.response_id


def test_zero_mad_is_unavailable_standardization_without_epsilon():
    data, q = inputs(count=5, response_values=(1,) * 5)
    frozen = retrieve(data, q, POLICY)
    r = response(data.target, 2)
    surprise = compare(frozen, r, r.available_ns + 1)
    assert surprise.signed_residual == 1 and surprise.signed_percentile == 1
    assert surprise.standardized_mad_residual is None and "ZERO_MAD" in surprise.reasons


@pytest.mark.parametrize("kind", ["missing", "value", "small"])
def test_missing_target_and_distribution_are_not_zero(kind):
    data, q = inputs(count=4 if kind == "small" else 7)
    frozen = retrieve(data, q, POLICY)
    r = None if kind == "missing" else response(data.target, None if kind == "value" else 1)
    surprise = compare(frozen, r, q.available_ns + 2)
    assert (
        surprise.signed_percentile
        is surprise.magnitude_percentile
        is surprise.signed_residual
        is None
    )
    assert surprise.state is not ResultState.AVAILABLE
    assert surprise.reasons


@pytest.mark.parametrize(
    "kind",
    ["before_frozen", "before_response", "same_completion", "event", "revision", "row", "metric"],
)
def test_expost_availability_and_exact_target_pins(kind):
    data, q = inputs()
    frozen = retrieve(data, q, POLICY)
    r = response(data.target, 1)
    available = r.available_ns + 1
    if kind == "before_frozen":
        available = q.available_ns - 1
    elif kind in ("before_response", "same_completion"):
        available = r.available_ns - (kind == "before_response")
    else:
        r = replace(
            r,
            **(
                {"event_id": "other"}
                if kind == "event"
                else {"revision": 2}
                if kind == "revision"
                else {"feature_row_id": "other"}
                if kind == "row"
                else {"spec": replace(r.spec, offset_ns=2)}
            ),
        )
    with pytest.raises(ValueError):
        compare(frozen, r, available)


def test_post_response_cannot_reselect_or_change_distances():
    data, q = inputs()
    frozen = retrieve(data, q, POLICY)
    members = frozen.candidates
    low = compare(frozen, response(data.target, -1), q.available_ns + 2)
    high = compare(frozen, response(data.target, 1), q.available_ns + 2)
    assert low.signed_percentile == 0 and high.signed_percentile == 1
    assert low.analogue_result_id == high.analogue_result_id == frozen.result_id
    assert frozen.candidates == members


def test_residual_overflow_and_distribution_overflow_remain_explicit():
    data, q = inputs(count=5, response_values=(-1.7e308,) * 5)
    frozen = retrieve(data, q, POLICY)
    s = compare(frozen, response(data.target, 1.7e308), q.available_ns + 2)
    assert s.signed_percentile == 1 and s.signed_residual is None
    assert "RESIDUAL_NUMERIC_OVERFLOW" in s.reasons
    d = distribution(
        SPEC,
        tuple((str(i), v) for i, v in enumerate((-1.7e308, -1.7e308, 1.7e308, 1.7e308, 1.7e308))),
        5,
    )
    assert d.median is None and d.reasons == ("NUMERIC_OVERFLOW",)


def test_tiny_nonzero_mad_does_not_force_finite_standardization():
    data, q = inputs(count=5, response_values=(0, 1e-308, 2e-308, 3e-308, 4e-308))
    s = compare(retrieve(data, q, POLICY), response(data.target, 1e308), q.available_ns + 2)
    assert s.standardized_mad_residual is None
    assert "STANDARDIZED_RESIDUAL_NUMERIC_OVERFLOW" in s.reasons
