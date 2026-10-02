"""Normal OLS uncertainty with shared parameter covariance and independent residuals."""

from math import fsum, isfinite, sqrt
from statistics import NormalDist

from eie.impact.models import (
    CounterfactualModelSpec,
    ModelFitResult,
    RegressionFit,
    UncertaintyInterval,
)

ASSUMPTIONS = (
    "DESCRIPTIVE_MODEL_BASED_NOT_CAUSAL",
    "NORMAL_APPROXIMATION_NOT_STUDENT_T",
    "HOMOSKEDASTIC_RESIDUALS",
    "INDEPENDENT_EVENT_PERIOD_RESIDUALS_FOR_CAR",
    "SHARED_FIT_PARAMETER_COVARIANCE_RETAINED_FOR_CAR",
    "FACTOR_BENCHMARK_INPUTS_TREATED_AS_FIXED",
)


def interval(center: float, variance: float, confidence: float) -> UncertaintyInterval | None:
    try:
        if not isfinite(variance) or variance < 0:
            return None
        se = sqrt(variance)
        width = NormalDist().inv_cdf((1 + confidence) / 2) * se
        lower, upper = center - width, center + width
        if not all(isfinite(v) for v in (se, lower, upper)):
            return None
        return UncertaintyInterval(confidence, se, lower, upper)
    except (ArithmeticError, ValueError):
        return None


def bands(
    fit: ModelFitResult,
    spec: CounterfactualModelSpec,
    design_sum: tuple[float, ...],
    expected: float,
    abnormal: float,
    count: int = 1,
) -> tuple[UncertaintyInterval | None, UncertaintyInterval | None]:
    p = fit.payload
    if (
        not isinstance(p, RegressionFit)
        or fit.observation_count < spec.minimum_uncertainty_observations
    ):
        return None, None
    try:
        terms = tuple(
            design_sum[i] * p.covariance[i][j] * design_sum[j]
            for i in range(len(design_sum))
            for j in range(len(design_sum))
        )
        parameter_variance = fsum(terms)
        if not isfinite(parameter_variance) or parameter_variance < -1e-12 * max(
            fsum(abs(v) for v in terms), 1e-300
        ):
            return None, None
        # Roundoff can make a positive semidefinite quadratic form barely negative.
        parameter_variance = max(0.0, parameter_variance)
        return (
            interval(expected, parameter_variance, spec.confidence_level),
            interval(
                abnormal, parameter_variance + count * p.residual_variance, spec.confidence_level
            ),
        )
    except (ArithmeticError, ValueError):
        return None, None
