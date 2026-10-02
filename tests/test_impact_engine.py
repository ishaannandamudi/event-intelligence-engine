from dataclasses import replace
from math import fsum, sin, sqrt

import pytest

from eie.core.errors import ValidationError
from eie.impact.engine import ImpactEngine, calculate, run_manifest
from eie.impact.estimation import fitting_periods
from eie.impact.inputs import FitExclusion
from eie.impact.models import (
    CARWindow,
    EstimationWindow,
    FitStatus,
    ImpactQuality,
    ModelType,
    RegressionFit,
)
from eie.impact.sensitivity import contamination_sensitivity, impact_set
from eie.impact.validation import ImpactValidationStatus, validate_impact, validate_set
from eie.persistence import codec
from eie.studies.market import MarketGap, Timeframe
from eie.studies.models import ReturnMethod
from examples.counterfactual_impact import CARS, ESTIMATION, factor_inputs
from examples.event_studies import MINUTE
from tests.impact_helpers import impact, inputs


def test_market_model_prior_fit_expected_abnormal_car_and_uncertainty(tmp_path):
    store, reader, (base, sector, _) = inputs(tmp_path)
    try:
        r = impact(base)
        assert r.fit.status is FitStatus.AVAILABLE
        assert r.fit.observation_count == 230
        assert isinstance(r.fit.payload, RegressionFit)
        alpha, beta = r.fit.payload.coefficients
        assert alpha == pytest.approx(0.00005, abs=1e-7)
        assert beta == pytest.approx(1.5, abs=1e-3)
        assert r.fit.payload.r_squared > 0.99
        assert r.fit.payload.residual_variance > 0
        for p in r.abnormal_curve:
            assert p.abnormal_return == pytest.approx(p.observed_return - p.expected_return)
            assert p.abnormal_uncertainty.lower <= p.abnormal_return <= p.abnormal_uncertainty.upper
        post = tuple(p for p in r.abnormal_curve if p.start_ns >= base.study.anchor_ns)
        assert r.cars[0].value == pytest.approx(fsum(p.abnormal_return for p in post))
        assert r.cars[0].period_count == 5
        assert r.cars[1].value == post[0].abnormal_return
        p = r.fit.payload
        design = tuple(fsum(pt.design[j] for pt in post) for j in range(2))
        expected_variance = 5 * p.residual_variance + sum(
            design[i] * p.covariance[i][j] * design[j] for i in range(2) for j in range(2)
        )
        assert r.cars[0].uncertainty.standard_error == pytest.approx(sqrt(expected_variance))
        assert validate_impact(r, reader).status is ImpactValidationStatus.VALID_WITH_WARNINGS
        assert codec.dumps(calculate(r.inputs, r.model_spec)) == codec.dumps(r)
        assert r.inputs.study == base.study and r.model_spec.schema_version == "1.0"
        other = impact(sector, ModelType.SECTOR_ADJUSTED)
        compare = impact_set((r, other))
        assert compare.sensitivity[0].difference_right_minus_left != 0
        assert validate_set(compare, reader)
    finally:
        store.close()


@pytest.mark.parametrize("kind", [ModelType.MARKET_ADJUSTED, ModelType.SECTOR_ADJUSTED])
def test_adjusted_expected_unit_benchmark_and_missing_benchmark(tmp_path, kind):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        r = impact(base, kind)
        assert r.fit.payload.coefficient == 1
        assert all(p.expected_uncertainty is None for p in r.abnormal_curve)
        missing = impact(replace(base, benchmark=None), kind)
        assert missing.fit.status is FitStatus.MISSING_INPUT
        assert ImpactQuality.MISSING_BENCHMARK in missing.quality
        assert all(c.value is None for c in missing.cars)
        bad = replace(
            base.benchmark,
            identity=replace(base.benchmark.identity, timeframe=Timeframe.DAILY),
            bars=tuple(replace(b, timeframe=Timeframe.DAILY) for b in base.benchmark.bars),
        )
        with pytest.raises(ValidationError, match="incompatible benchmark"):
            impact(replace(base, benchmark=bad), kind)
        bad = replace(base.benchmark, sessions=())
        with pytest.raises(ValidationError, match="session semantics"):
            impact(replace(base, benchmark=bad), kind)
    finally:
        store.close()


def test_future_bars_do_not_enter_fit_or_projected_result(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        r = impact(base)
        fit_periods = fitting_periods(r.inputs, r.inputs.target, r.model_spec)
        assert all(
            p.end_ns < base.study.anchor_ns and p.available_ns < base.study.anchor_ns
            for p in fit_periods
        )
        end = base.study.post_end_ns
        changed = replace(
            base.benchmark,
            bars=tuple(
                replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
                if b.end_ns > end
                else b
                for b in base.benchmark.bars
            ),
        )
        assert impact(replace(base, benchmark=changed)) == r
        # The target's supplied event-window shock does not affect the prior fit.
        cutoff = base.study.anchor_ns
        late = replace(
            base.benchmark,
            bars=tuple(
                replace(b, available_ns=cutoff)
                if cutoff - 100 * MINUTE <= b.end_ns <= cutoff - 90 * MINUTE
                else b
                for b in base.benchmark.bars
            ),
        )
        rlate = impact(replace(base, benchmark=late))
        assert rlate.fit.observation_count == r.fit.observation_count - 12
        assert rlate.fit.missing_pair_count == 12
        assert ImpactQuality.DATA_GAP in rlate.quality
        earlier = impact(base, fit_cutoff_ns=base.study.anchor_ns - 50 * MINUTE)
        assert all(
            p.end_ns < earlier.model_spec.fit_cutoff_ns
            for p in fitting_periods(earlier.inputs, earlier.inputs.target, earlier.model_spec)
        )
        with pytest.raises(ValidationError):
            impact(base, fit_cutoff_ns=cutoff + 1)
    finally:
        store.close()


def test_missing_paired_periods_zero_variance_and_small_sample(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        at = base.study.anchor_ns
        missing = replace(
            base.benchmark,
            bars=tuple(b for b in base.benchmark.bars if b.end_ns != at - 100 * MINUTE),
        )
        r = impact(replace(base, benchmark=missing))
        assert r.fit.observation_count == 228 and r.fit.missing_pair_count == 2
        flat = replace(
            base.benchmark,
            bars=tuple(
                replace(b, open=100, high=100, low=100, close=100) for b in base.benchmark.bars
            ),
        )
        failed = impact(replace(base, benchmark=flat))
        assert failed.fit.status is FitStatus.DEGENERATE
        assert ImpactQuality.DEGENERATE_BENCHMARK in failed.quality
        assert all(c.value is None for c in failed.cars)
        short_window = EstimationWindow(
            -25 * MINUTE, -20 * MINUTE, 16 * MINUTE, ESTIMATION.session_policy
        )
        short = ImpactEngine().impact_event(
            base, ModelType.MARKET_MODEL, short_window, CARS, minimum_observations=2
        )
        assert short.fit.status is FitStatus.AVAILABLE and short.fit.observation_count == 5
        assert ImpactQuality.UNCERTAINTY_UNAVAILABLE in short.quality
        assert short.cars[0].value is not None and short.cars[0].uncertainty is None
        default = ImpactEngine().impact_event(base, ModelType.MARKET_MODEL, short_window, CARS)
        assert default.fit.status is FitStatus.INSUFFICIENT
    finally:
        store.close()


@pytest.mark.parametrize("method", list(ReturnMethod))
def test_car_is_independent_sum_of_period_residuals_not_cumulative_curve(tmp_path, method):
    store, _, (base, _, _) = inputs(tmp_path, method=method)
    try:
        r = impact(base, ModelType.MARKET_ADJUSTED)
        post = [p for p in r.abnormal_curve if p.offset_ns > 0]
        assert r.cars[0].value == pytest.approx(fsum(p.abnormal_return for p in post))
        assert r.cars[1].value == post[0].abnormal_return
        assert r.cars[0].value != sum(p.value for p in base.study.curve if p.offset_ns > 0)
        assert r.model_spec.semantics.method is method
    finally:
        store.close()


def test_factors_single_multifactor_singular_missing_and_availability(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        supplied = factor_inputs(base)
        market = impact(base)
        single = impact(supplied, ModelType.SUPPLIED_FACTOR_MODEL)
        assert single.fit.payload.coefficients == market.fit.payload.coefficients
        assert single.cars[0].value == market.cars[0].value
        f = supplied.factors[0]
        other = replace(
            f,
            factor_id="size",
            dataset_id="factor-size",
            observations=tuple(
                replace(o, value=sin(j * 0.17) * 0.001) for j, o in enumerate(f.observations)
            ),
        )
        multi = impact(replace(supplied, factors=(f, other)), ModelType.SUPPLIED_FACTOR_MODEL)
        assert multi.fit.status is FitStatus.AVAILABLE and len(multi.fit.payload.coefficients) == 3
        singular = impact(
            replace(supplied, factors=(f, replace(f, factor_id="same"))),
            ModelType.SUPPLIED_FACTOR_MODEL,
        )
        assert (
            singular.fit.status is FitStatus.SINGULAR
            and ImpactQuality.SINGULAR_FACTORS in singular.quality
        )
        missing = impact(replace(supplied, factors=()), ModelType.SUPPLIED_FACTOR_MODEL)
        assert missing.fit.status is FitStatus.MISSING_INPUT
        assert ImpactQuality.MISSING_FACTORS in missing.quality
        anchor = base.study.anchor_ns
        late = replace(
            f,
            observations=tuple(
                replace(o, available_ns=anchor)
                if anchor - 100 * MINUTE <= o.end_ns <= anchor - 90 * MINUTE
                else o
                for o in f.observations
            ),
        )
        causal = impact(replace(supplied, factors=(late,)), ModelType.SUPPLIED_FACTOR_MODEL)
        assert causal.fit.observation_count == 219 and causal.fit.missing_pair_count == 11
        event_missing = replace(
            f,
            observations=tuple(
                replace(o, value=None) if o.end_ns == anchor + MINUTE else o for o in f.observations
            ),
        )
        incomplete = impact(
            replace(supplied, factors=(event_missing,)), ModelType.SUPPLIED_FACTOR_MODEL
        )
        assert incomplete.cars[0].value is None and incomplete.fit == single.fit
        with pytest.raises(ValidationError):
            impact(
                replace(
                    supplied,
                    factors=(replace(f, semantics=replace(f.semantics, method=ReturnMethod.LOG)),),
                ),
                ModelType.SUPPLIED_FACTOR_MODEL,
            )
    finally:
        store.close()


def test_contamination_both_estimation_exclusion_and_model_identity(tmp_path):
    store, _, (base, sector, _) = inputs(tmp_path)
    try:
        r = impact(base)
        assert ImpactQuality.TARGET_CONTAMINATION in r.quality
        both = contamination_sensitivity(r)
        assert validate_set(both)
        assert len(both.results) == 2
        excluded = next(v for v in both.results if ImpactQuality.EVENT_EXCLUDED in v.quality)
        assert all(
            p.observed_return is not None and p.expected_return is None
            for p in excluded.abnormal_curve
        )
        assert any("CONTAMINATION_POLICY_COMPARISON" in p.reasons for p in both.sensitivity)
        at = base.study.anchor_ns
        x = FitExclusion(
            base.target.identity.instrument_id,
            at - 100 * MINUTE,
            at - 90 * MINUTE,
            at - 100 * MINUTE,
            "other-event",
            "SUPPLIED",
        )
        supplied = replace(base, fit_exclusions=(x,))
        fit = impact(supplied, exclude_fit_contamination=True)
        assert fit.fit.excluded_count == 10 and fit.fit.observation_count == 220
        assert ImpactQuality.FIT_CONTAMINATION_EXCLUDED in fit.quality
        assert impact(supplied).fit == r.fit
        future = impact(
            replace(base, fit_exclusions=(replace(x, available_ns=at),)),
            exclude_fit_contamination=True,
        )
        assert future.fit.excluded_count == 0
        variants = (impact(sector), impact(base, model_version="new-explicit-version"), fit)
        assert all(v.model_spec.model_id != r.model_spec.model_id for v in variants)
        assert run_manifest((r, *variants)) == run_manifest(tuple(reversed((r, *variants))))
    finally:
        store.close()


def test_gap_in_event_window_missing_car_and_tamper_validation(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        missing = replace(
            base.benchmark,
            gaps=(MarketGap(base.study.anchor_ns, base.study.anchor_ns + MINUTE, "missing"),),
        )
        r = impact(replace(base, benchmark=missing))
        assert r.cars[0].value is None
        assert ImpactQuality.MISSING_EVENT_WINDOW_DATA in r.quality
        good = impact(base)
        tampered = replace(good, fit=replace(good.fit, missing_pair_count=10))
        assert validate_impact(tampered).status is ImpactValidationStatus.INVALID
        with pytest.raises(ValidationError):
            impact(replace(base, study=replace(base.study, post_return=100)))
        with pytest.raises(ValidationError):
            calculate(good.inputs, replace(good.model_spec, universe=("other",)))
        with pytest.raises(ValidationError):
            calculate(good.inputs, replace(good.model_spec, engine_version="future"))
        with pytest.raises(ValidationError):
            ImpactEngine().impact_events(((base, ModelType.MARKET_MODEL, ESTIMATION, CARS),) * 2)
        assert ImpactEngine().impact_events(
            ((base, ModelType.MARKET_MODEL, ESTIMATION, CARS),)
        ) == (good,)
        with pytest.raises(ValidationError):
            ImpactEngine().impact_events(())
        with pytest.raises(ValidationError):
            run_manifest(())
        with pytest.raises(ValidationError):
            impact_set((good, good))
        shifted = replace(
            good,
            cars=tuple(
                replace(
                    c, window=CARWindow(c.window.label, c.window.start_offset, c.window.end_offset)
                )
                for c in good.cars
            ),
        )
        assert shifted == good
    finally:
        store.close()
