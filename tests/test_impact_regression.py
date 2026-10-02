from math import isfinite, sin, sqrt

import pytest

from eie.impact.models import FitStatus
from eie.impact.regression import ols
from eie.impact.uncertainty import interval


def regression(y, x, *, intercept=True, minimum=30, condition=1e8):
    return ols(
        tuple(y),
        tuple(x),
        tuple(f"x{j}" for j in range(len(x[0]) if x else 1)),
        intercept=intercept,
        minimum=minimum,
        maximum_condition=condition,
    )


def test_closed_form_alpha_beta_residual_variance_and_standard_errors():
    x = tuple((float(j),) for j in range(40))
    residual = tuple((-1.0 if j % 4 in (0, 3) else 1.0) * 0.2 for j in range(40))
    y = tuple(0.5 + 2 * row[0] + e for row, e in zip(x, residual, strict=True))
    status, fit = regression(y, x)
    assert status is FitStatus.AVAILABLE
    assert fit.coefficients == pytest.approx((0.5, 2), abs=1e-12)
    assert fit.residuals == pytest.approx(residual)
    assert fit.residual_variance == pytest.approx(sum(e * e for e in residual) / 38)
    sxx = sum((j - 19.5) ** 2 for j in range(40))
    assert fit.standard_errors[1] == pytest.approx(sqrt(fit.residual_variance / sxx))
    assert fit.standard_errors[0] == pytest.approx(
        sqrt(fit.residual_variance * (1 / 40 + 19.5**2 / sxx))
    )
    assert fit.r_squared == pytest.approx(
        1 - sum(e * e for e in residual) / sum((v - sum(y) / 40) ** 2 for v in y)
    )
    assert fit.degrees_of_freedom == 38 and fit.condition_number >= 1


def test_multifactor_known_coefficients_and_centering():
    x = tuple((sin(j * 0.31), sin(j * 0.67)) for j in range(90))
    status, fit = regression(tuple(0.1 + 1.2 * a - 0.7 * b for a, b in x), x)
    assert status is FitStatus.AVAILABLE
    assert fit.coefficients == pytest.approx((0.1, 1.2, -0.7))
    assert fit.r_squared == pytest.approx(1)
    assert max(abs(e) for e in fit.residuals) < 1e-14


def test_no_intercept_and_constant_target():
    x = tuple((float(j + 1),) for j in range(40))
    _, fit = regression(tuple(2 * a for (a,) in x), x, intercept=False)
    assert fit.names == ("x0",) and fit.coefficients == pytest.approx((2,))
    _, fit = regression((0.1,) * 40, x)
    assert fit.r_squared is None and fit.coefficients == pytest.approx((0.1, 0))


@pytest.mark.parametrize("count,minimum", [(1, 2), (2, 2), (29, 30)])
def test_insufficient_observations(count, minimum):
    assert regression(
        tuple(float(j) for j in range(count)),
        tuple((float(j),) for j in range(count)),
        minimum=minimum,
    ) == (FitStatus.INSUFFICIENT, None)


@pytest.mark.parametrize("intercept", [True, False])
@pytest.mark.parametrize("constant", [0.0, 0.01])
def test_zero_variance(intercept, constant):
    assert regression((0.1,) * 40, ((0.0,),) * 40, intercept=intercept) == (
        FitStatus.DEGENERATE,
        None,
    )
    assert regression((0.1,) * 40, ((constant,),) * 40, intercept=intercept) == (
        FitStatus.DEGENERATE,
        None,
    )


def test_singular_and_nearly_singular_design():
    x = tuple((float(j), float(j) * 2) for j in range(40))
    assert regression(tuple(float(j) for j in range(40)), x)[0] is FitStatus.SINGULAR
    near = tuple((sin(j), sin(j) + 1e-7 * sin(2 * j)) for j in range(40))
    assert (
        regression(tuple(sin(j) for j in range(40)), near, condition=1e5)[0] is FitStatus.SINGULAR
    )


def test_extreme_numerics_fail_closed_and_tiny_finite_series_are_stable():
    assert (
        regression((1e308, -1e308) * 20, ((1e308,), (-1e308,)) * 20)[0]
        is FitStatus.NUMERIC_UNAVAILABLE
    )
    x = tuple((j * 1e-100,) for j in range(40))
    status, fit = regression(tuple(j * 2e-100 for j in range(40)), x)
    # Centering/scaling also handles small finite series.
    assert status is FitStatus.AVAILABLE
    assert fit.coefficients == pytest.approx((0, 2), abs=1e-12)
    assert all(isfinite(v) for v in fit.coefficients)
    tiny = tuple((j * 1e-200,) for j in range(40))
    assert (
        regression(tuple(j * 2e-200 for j in range(40)), tiny)[0] is FitStatus.NUMERIC_UNAVAILABLE
    )
    assert (
        regression((float("inf"),) * 40, tuple((float(j),) for j in range(40)))[0]
        is FitStatus.NUMERIC_UNAVAILABLE
    )
    assert (
        regression((1e308, -1e308) * 20, ((1.0,), (-1.0,)) * 20)[0] is FitStatus.NUMERIC_UNAVAILABLE
    )


def test_normal_intervals_explicit_confidence_and_nonfinite_guard():
    band = interval(0.02, 0.0001, 0.95)
    assert band.standard_error == pytest.approx(0.01)
    assert band.lower == pytest.approx(0.00040036015459946)
    assert band.upper == pytest.approx(0.03959963984540054)
    assert interval(0, -1, 0.95) is None
    assert interval(0, float("inf"), 0.95) is None
    assert interval(float("inf"), 1, 0.95) is None
    assert interval(1e308, 1e308, 0.99999999999999) is not None
