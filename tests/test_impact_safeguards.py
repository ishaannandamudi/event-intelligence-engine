from dataclasses import replace
from math import sin

import pytest

from eie.backfill.models import ReconstructionQuality
from eie.core.errors import ValidationError
from eie.impact.engine import ImpactEngine, calculate, run_manifest
from eie.impact.estimation import estimation_bounds, period_returns
from eie.impact.inputs import ImpactInputs
from eie.impact.models import CARWindow, EstimationWindow, FitStatus, ImpactQuality, ModelType
from eie.impact.sensitivity import impact_set
from eie.impact.uncertainty import bands, interval
from eie.impact.validation import ImpactValidationStatus, validate_impact
from eie.persistence import codec
from eie.studies.engine import calculate as raw_study
from eie.studies.market import (
    Adjustment,
    MarketBar,
    MarketGap,
    MarketQuote,
    MarketSession,
    MarketSlice,
    SplitBoundary,
    Timeframe,
)
from eie.studies.models import SessionPolicy, StudyQuality
from examples.counterfactual_impact import CARS, ESTIMATION, fixture
from examples.event_studies import DAY, MINUTE
from tests.impact_helpers import impact, inputs
from tests.study_helpers import inputs_history
from tests.test_study_history import record


def rebuilt(base, data, *, window=None, policy=None):
    from eie.studies.engine import visible

    definition = replace(
        base.study.definition,
        window=window or base.study.definition.window,
        policy=policy or base.study.definition.policy,
    )
    study = raw_study(
        definition, visible(data, definition.data_as_of_ns), (), base.study.contamination
    )
    return replace(base, target=data, study=study)


def test_alternate_estimation_sensitivity_and_unchanged_model_disagreement(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        a = impact(base)
        window = replace(ESTIMATION, start_offset=-100 * MINUTE)
        b = ImpactEngine().impact_event(base, ModelType.MARKET_MODEL, window, CARS)
        comparison = impact_set((a, b))
        assert len(comparison.sensitivity) == 3
        assert any(p.difference_right_minus_left != 0 for p in comparison.sensitivity)
        assert comparison.results == tuple(sorted((a, b), key=lambda r: r.impact_id))
        assert a.model_spec.model_id != b.model_spec.model_id
        alternative = replace(a.model_spec, intercept=False)
        r = calculate(a.inputs, alternative)
        assert r.fit.status is FitStatus.AVAILABLE and len(r.fit.payload.coefficients) == 1
        assert all(p.expected_uncertainty is not None for p in r.abnormal_curve)
    finally:
        store.close()


def test_trading_session_clocks_use_supplied_prior_calendar(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        w = replace(base.study.definition.window, session_policy=SessionPolicy.TRADING)
        base = rebuilt(base, base.target, window=w)
        r = ImpactEngine().impact_event(
            base,
            ModelType.MARKET_MODEL,
            replace(ESTIMATION, session_policy=SessionPolicy.TRADING),
            CARS,
        )
        assert r.fit.observation_count == 230
        assert r.cars[0].period_count == 5
        assert tuple(p.offset_ns for p in r.abnormal_curve) == tuple(
            j * MINUTE for j in range(-3, 6)
        )
        with pytest.raises(ValidationError):
            estimation_bounds(
                base.study,
                replace(
                    ESTIMATION, start_offset=-1000 * MINUTE, session_policy=SessionPolicy.TRADING
                ),
                base.target.sessions,
            )
    finally:
        store.close()


def test_daily_returns_same_frequency_application_and_car(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        at = base.study.anchor_ns

        def daily(data, beta):
            price = 100.0
            bars = []
            for j in range(-7, 3):
                old, price = price, price * (1 + beta * 0.01 * sin(j))
                bars.append(
                    MarketBar(
                        data.identity.instrument_id,
                        at + (j - 1) * DAY,
                        at + j * DAY,
                        old,
                        max(old, price),
                        min(old, price),
                        price,
                        100,
                        at + j * DAY,
                        data.identity.source,
                        Timeframe.DAILY,
                    )
                )
            return MarketSlice(
                replace(data.identity, timeframe=Timeframe.DAILY),
                tuple(bars),
                sessions=(
                    MarketSession(
                        "daily-continuous", bars[0].start_ns, bars[-1].end_ns, 0, "SYNTHETIC", "1"
                    ),
                ),
            )

        target, market = daily(base.target, 1.5), daily(base.benchmark, 1)
        window = replace(
            base.study.definition.window,
            timeframe=Timeframe.DAILY,
            pre_ns=2 * DAY,
            post_ns=2 * DAY,
            offsets_ns=(-DAY, 0, DAY, 2 * DAY),
        )
        definition = replace(
            base.study.definition,
            window=window,
            data_as_of_ns=at + 2 * DAY,
            policy=replace(base.study.definition.policy, max_lag_ns=DAY),
        )
        study = raw_study(definition, target, (), base.study.contamination)
        r = ImpactEngine().impact_event(
            ImpactInputs(study, target, market),
            ModelType.MARKET_MODEL,
            EstimationWindow(-6 * DAY, -3 * DAY, DAY, SessionPolicy.ELAPSED),
            (CARWindow("post", 0, 2 * DAY),),
            minimum_observations=2,
        )
        assert r.fit.observation_count == 3 and r.fit.payload.coefficients == pytest.approx(
            (0, 1.5), abs=1e-13
        )
        assert r.cars[0].period_count == 2
    finally:
        store.close()


def test_corporate_action_period_gaps_partial_bars_and_no_fake_zero(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        at = base.study.anchor_ns
        adjusted = replace(
            base.target,
            identity=replace(base.target.identity, adjustment=Adjustment.UNADJUSTED),
            splits=(SplitBoundary(at + MINUTE, at, "SUPPLIED", "split-1"),),
        )
        raw = rebuilt(base, adjusted)
        benchmark = replace(
            base.benchmark,
            identity=replace(base.benchmark.identity, adjustment=Adjustment.UNADJUSTED),
        )
        r = impact(replace(raw, benchmark=benchmark))
        assert ImpactQuality.CORPORATE_ACTION_RISK in r.quality
        assert next(p for p in r.abnormal_curve if p.end_ns == at + MINUTE).observed_return is None
        assert r.cars[0].value is None
        closed = replace(
            base.benchmark,
            bars=tuple(b for b in base.benchmark.bars if b.end_ns != at - 100 * MINUTE),
        )
        periods = period_returns(closed, r.model_spec.semantics, at)
        assert any(p.value is None for p in periods)
        partial = replace(
            base.benchmark,
            bars=tuple(
                replace(b, start_ns=b.start_ns + 1) if b.end_ns == at + MINUTE else b
                for b in base.benchmark.bars
            ),
        )
        failed = impact(replace(base, benchmark=partial))
        assert failed.cars[0].value is None
        gaps = replace(
            base.benchmark, gaps=(MarketGap(at - 30 * MINUTE, at - 29 * MINUTE, "prior-gap"),)
        )
        assert impact(replace(base, benchmark=gaps)).fit.missing_pair_count == 1
    finally:
        store.close()


def test_supplied_control_quote_metric_uses_comparable_windows(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        at = supplied.study.anchor_ns

        def quoted(data, width):
            return replace(
                data,
                quotes=tuple(
                    MarketQuote(
                        data.identity.instrument_id,
                        t,
                        t,
                        100,
                        100 + width,
                        10,
                        20,
                        data.identity.source,
                    )
                    for t in (at, at + 5 * MINUTE)
                ),
            )

        target = quoted(supplied.target, 0.2)
        supplied = rebuilt(supplied, target)
        supplied = replace(
            supplied,
            controls=tuple(replace(c, market=quoted(c.market, 0.1)) for c in supplied.controls),
        )
        r = impact(supplied, ModelType.MATCHED_CONTROLS)
        spreads = [m for m in r.metrics if m.metric == "QUOTED_SPREAD"]
        assert len(spreads) == 1 and spreads[0].deviation == pytest.approx(0.1)
        excluded = impact(supplied, ModelType.MATCHED_CONTROLS, exclude_contaminated=True)
        assert excluded.metrics == ()
    finally:
        store.close()


def test_no_prior_controls_action_risk_and_low_fit_quality(tmp_path):
    store, _, (base, _, supplied) = inputs(tmp_path)
    try:
        target = replace(
            supplied.target,
            identity=replace(supplied.target.identity, adjustment=Adjustment.UNADJUSTED),
        )
        supplied = rebuilt(supplied, target)
        supplied = replace(
            supplied,
            controls=tuple(
                replace(
                    c,
                    market=replace(
                        c.market,
                        identity=replace(c.market.identity, adjustment=Adjustment.UNADJUSTED),
                    ),
                )
                for c in supplied.controls
            ),
        )
        r = impact(supplied, ModelType.MATCHED_CONTROLS)
        assert r.fit.status is FitStatus.INSUFFICIENT
        assert all(
            reason == "UNRESOLVED_CORPORATE_ACTION_RISK" for _, reason in r.fit.payload.exclusions
        )
        flat = replace(
            base.target,
            bars=tuple(
                replace(b, open=100, high=100, low=100, close=100) for b in base.target.bars
            ),
        )
        poor = impact(rebuilt(base, flat))
        assert poor.fit.payload.r_squared is None and ImpactQuality.LOW_R2 in poor.quality
    finally:
        store.close()


def test_future_event_correction_never_changes_historical_model_or_fingerprint(tmp_path):
    at = 9 * DAY + 10 * MINUTE
    original, corrected = record(at), record(at + 20 * MINUTE, revision=2)
    store, reader = inputs_history(tmp_path, (original, corrected))
    try:
        base, _, _ = fixture(reader, original.record.event_id)
        r = impact(base)
        assert r.inputs.study.definition.event.revision.number == 1
        assert validate_impact(r, reader).status is ImpactValidationStatus.VALID
        assert codec.dumps(impact(fixture(reader, original.record.event_id)[0])) == codec.dumps(r)
    finally:
        store.close()


def test_input_provenance_raw_prices_calendar_and_validation_fail_closed(tmp_path):
    store, reader, (base, _, _) = inputs(tmp_path)
    try:
        r = impact(base)
        at = base.study.anchor_ns
        bad = replace(
            base.target,
            bars=tuple(replace(b, volume=101) if b.end_ns == at else b for b in base.target.bars),
        )
        with pytest.raises(ValidationError, match="raw study"):
            impact(replace(base, target=bad))
        with pytest.raises(ValidationError, match="raw study"):
            impact(replace(base, target=replace(base.target, sessions=())))
        bad = replace(r.model_spec, study_fingerprint="b" * 64)
        assert validate_impact(rebind(r, bad)).status is ImpactValidationStatus.INVALID
        fake_reader = replace_reader(reader)
        assert validate_impact(r, fake_reader).status is ImpactValidationStatus.INVALID
        with pytest.raises(ValidationError):
            impact(replace(base, factors=fixture_factors(base)))
        with pytest.raises(ValidationError):
            impact_set((r, rebind(r, replace(r.model_spec, study_fingerprint="b" * 64))))
        with pytest.raises(ValidationError):
            run_manifest((r, rebind(r, replace(r.model_spec, event_build_id="other"))))
    finally:
        store.close()


def rebind(result, spec):
    return replace(
        result,
        model_spec=spec,
        abnormal_curve=tuple(replace(p, model_id=spec.model_id) for p in result.abnormal_curve),
    )


def replace_reader(reader):
    from copy import copy

    other = copy(reader)
    other.build_id = "other"
    return other


def fixture_factors(base):
    from examples.counterfactual_impact import factor_inputs

    return factor_inputs(base).factors


def test_reconstruction_and_coarse_timing_flags_propagate(tmp_path):
    from eie.impact.diagnostics import quality_flags

    store, _, (base, _, _) = inputs(tmp_path)
    try:
        r = impact(base)
        flagged = replace(
            base.study,
            definition=replace(
                base.study.definition,
                reconstruction_quality=ReconstructionQuality.SOURCE_TIMESTAMP_RECONSTRUCTED,
            ),
            quality=base.study.quality | {StudyQuality.COARSE_EVENT_TIME},
        )
        flags = quality_flags(replace(base, study=flagged), r.model_spec, r.fit)
        assert {ImpactQuality.COARSE_EVENT_TIME, ImpactQuality.RECONSTRUCTION_LIMITATION} <= flags
    finally:
        store.close()


def test_uncertainty_extreme_condition_and_invalid_quadratic_fail_closed(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        r = impact(base)
        assert interval(0, 1, 0.9999999999999999) is None
        assert interval(1e308, 1e308, 0.95) is not None
        assert bands(r.fit, r.model_spec, (1e308, 1e308), 0, 0) == (None, None)
        negative = replace(
            r.fit, payload=replace(r.fit.payload, covariance=((-1.0, 0.0), (0.0, -1.0)))
        )
        assert bands(negative, r.model_spec, (1, 1), 0, 0) == (None, None)
    finally:
        store.close()


def test_nonfinite_factor_prediction_is_unavailable_with_finite_prior_fit(tmp_path):
    from examples.counterfactual_impact import factor_inputs

    store, _, (base, _, _) = inputs(tmp_path)
    try:
        supplied = factor_inputs(base)
        original = supplied.factors[0]
        meaning = original.semantics
        target = {
            (p.start_ns, p.end_ns): p.value
            for p in period_returns(base.target, meaning, base.study.definition.data_as_of_ns)
        }
        first = replace(
            original,
            observations=tuple(replace(o, value=o.value / 100) for o in original.observations),
        )
        second = replace(
            first,
            factor_id="other",
            observations=tuple(
                replace(o, value=o.value - target[(o.start_ns, o.end_ns)] / 2)
                for o in first.observations
            ),
        )
        at = base.study.anchor_ns + MINUTE
        factors = tuple(
            replace(
                f,
                observations=tuple(
                    replace(o, value=1e308) if o.end_ns == at else o for o in f.observations
                ),
            )
            for f in (first, second)
        )
        result = impact(replace(supplied, factors=factors), ModelType.SUPPLIED_FACTOR_MODEL)
        assert result.fit.status is FitStatus.AVAILABLE
        assert result.fit.payload.coefficients == pytest.approx((0, 2, -2), abs=1e-10)
        point = next(p for p in result.abnormal_curve if p.end_ns == at)
        assert point.expected_return is None and point.abnormal_return is None
        assert ImpactQuality.NUMERIC_UNAVAILABLE in point.quality
        assert result.cars[0].value is None
    finally:
        store.close()
