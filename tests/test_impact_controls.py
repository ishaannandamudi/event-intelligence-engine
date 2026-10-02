from dataclasses import replace
from math import fsum

import pytest

from eie.impact.inputs import ControlCandidate
from eie.impact.models import (
    ControlFit,
    FitStatus,
    ImpactQuality,
    MatchingPolicy,
    ModelType,
    WeightMethod,
)
from eie.studies.market import Adjustment, ReturnType
from examples.counterfactual_impact import feature
from examples.event_studies import MINUTE
from tests.impact_helpers import impact, inputs


def test_top_k_standardized_distance_population_and_equal_basket(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        r = impact(supplied, ModelType.MATCHED_CONTROLS, matching=MatchingPolicy(k=2))
        p = r.fit.payload
        assert isinstance(p, ControlFit)
        assert tuple(c.instrument_id for c in p.selected) == ("control_a", "control_b")
        assert tuple(c.weight for c in p.selected) == (0.5, 0.5)
        assert p.scaling_members == ("control_a", "control_b", "control_c")
        assert p.matching_ns < supplied.study.anchor_ns
        assert p.scales[1][1] == pytest.approx(
            (sum((v - 40 / 3) ** 2 for v in (9, 11, 20)) / 3) ** 0.5
        )
        assert p.selected[0].distance == pytest.approx(p.selected[1].distance)
        assert r.metrics[0].metric == "REALIZED_LOG_VOLATILITY"
        assert r.metrics[0].deviation == pytest.approx(
            r.metrics[0].observed - r.metrics[0].expected
        )
        assert all(p.expected_return is not None for p in r.abnormal_curve)
        assert r.cars[0].uncertainty is None
    finally:
        store.close()


@pytest.mark.parametrize("weighting", list(WeightMethod))
def test_explicit_weights_zero_distance_and_feature_provenance(tmp_path, weighting):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        at = supplied.study.anchor_ns
        controls = tuple(
            replace(c, features=feature(c.instrument_id, at, n))
            for c, n in zip(supplied.controls, (10, 11, 20), strict=True)
        )
        r = impact(
            replace(supplied, controls=controls),
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2, weighting=weighting),
        )
        selected = r.fit.payload.selected
        assert selected[0].distance == 0
        assert tuple(c.weight for c in selected) == (
            (0.5, 0.5) if weighting is WeightMethod.EQUAL else (1.0, 0.0)
        )
        assert all(c.features.dataset_id.startswith("features-") for c in selected)
        uneven = tuple(
            replace(c, features=feature(c.instrument_id, at, n))
            for c, n in zip(supplied.controls, (11, 13, 20), strict=True)
        )
        other = impact(
            replace(supplied, controls=uneven),
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2, weighting=WeightMethod.INVERSE_DISTANCE),
        )
        assert tuple(c.weight for c in other.fit.payload.selected) == pytest.approx((0.75, 0.25))
        assert fsum(c.weight for c in other.fit.payload.selected) == 1
    finally:
        store.close()


def test_eligibility_reasons_target_missing_data_features_and_semantics(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        anchor = supplied.study.anchor_ns
        original = supplied.controls[0]
        cases = [
            ControlCandidate(
                supplied.target.identity.instrument_id, supplied.target, supplied.target_features
            ),
            ControlCandidate("missing_market", None, None),
        ]

        def candidate(name, **kwargs):
            c = replace(
                original,
                instrument_id=name,
                market=replace(
                    original.market,
                    identity=replace(original.market.identity, instrument_id=name),
                    bars=tuple(replace(b, instrument_id=name) for b in original.market.bars),
                ),
                features=feature(name, anchor, 10),
            )
            return replace(c, **kwargs)

        cases.append(candidate("missing_features", features=None))
        c = candidate("incompatible_features")
        cases.append(replace(c, features=replace(c.features, values=(("different", 1.0),))))
        c = candidate("incompatible_returns")
        cases.append(
            replace(
                c,
                market=replace(
                    c.market,
                    identity=replace(
                        c.market.identity,
                        adjustment=Adjustment.SPLIT_DIVIDEND_ADJUSTED,
                        return_type=ReturnType.TOTAL_RETURN,
                    ),
                ),
            )
        )
        c = candidate("unadjusted")
        cases.append(
            replace(
                c,
                market=replace(
                    c.market, identity=replace(c.market.identity, adjustment=Adjustment.UNADJUSTED)
                ),
            )
        )
        c = candidate("insufficient")
        cases.append(
            replace(
                c,
                market=replace(
                    c.market, bars=tuple(b for b in c.market.bars if b.end_ns >= anchor)
                ),
            )
        )
        c = candidate("future_features")
        cases.append(replace(c, features=replace(c.features, available_ns=anchor)))
        controls = tuple(sorted((*supplied.controls, *cases), key=lambda c: c.instrument_id))
        r = impact(replace(supplied, controls=controls), ModelType.MATCHED_CONTROLS)
        reasons = dict(r.fit.payload.exclusions)
        assert reasons == {
            "security_fixture": "TARGET_EXCLUDED",
            "missing_market": "MISSING_MARKET",
            "missing_features": "MISSING_PRIOR_FEATURES",
            "future_features": "MISSING_PRIOR_FEATURES",
            "incompatible_features": "INCOMPATIBLE_FEATURES",
            "incompatible_returns": "INCOMPATIBLE_RETURNS",
            "insufficient": "INSUFFICIENT_PRIOR_MARKET",
            "unadjusted": "INCOMPATIBLE_RETURNS",
        }
        assert ImpactQuality.POOR_CONTROL_MATCH in r.quality
        assert all(
            c.instrument_id != supplied.target.identity.instrument_id
            for c in r.fit.payload.selected
        )
        assert all(
            c.features is None for c in r.inputs.controls if c.instrument_id == "future_features"
        )
    finally:
        store.close()


def test_contaminated_controls_deterministic_exclusion_or_flag(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        controls = tuple(
            replace(c, contamination=supplied.study.contamination)
            if c.instrument_id == "control_a"
            else c
            for c in supplied.controls
        )
        supplied = replace(supplied, controls=controls)
        exclude = impact(supplied, ModelType.MATCHED_CONTROLS, matching=MatchingPolicy(k=2))
        assert tuple(c.instrument_id for c in exclude.fit.payload.selected) == (
            "control_b",
            "control_c",
        )
        assert ("control_a", "CONTROL_CONTAMINATION") in exclude.fit.payload.exclusions
        include = impact(
            supplied,
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2, exclude_contaminated=False),
        )
        assert include.fit.payload.selected[0].instrument_id == "control_a"
        assert include.fit.payload.selected[0].contaminated
        assert ImpactQuality.CONTROL_CONTAMINATION in include.quality
        future_flags = tuple(
            replace(f, available_ns=supplied.study.definition.selection_as_of_ns + 1)
            for f in supplied.study.contamination
        )
        future = impact(
            replace(
                supplied, controls=tuple(replace(c, contamination=future_flags) for c in controls)
            ),
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2),
        )
        assert future.fit.payload.selected[0].instrument_id == "control_a"
    finally:
        store.close()


def test_prior_scaling_future_features_cannot_change_result_and_missing_target(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        anchor = supplied.study.anchor_ns
        c = supplied.controls[-1]
        future = replace(c, features=replace(c.features, available_ns=anchor + MINUTE))
        a = impact(
            replace(supplied, controls=(*supplied.controls[:2], future)), ModelType.MATCHED_CONTROLS
        )
        b = impact(
            replace(
                supplied,
                controls=(
                    *supplied.controls[:2],
                    replace(
                        future,
                        features=replace(
                            future.features, values=(("beta", 1e200), ("size", 1e200))
                        ),
                    ),
                ),
            ),
            ModelType.MATCHED_CONTROLS,
        )
        assert codec_bytes(a) == codec_bytes(b)
        assert a.fit.payload.scaling_members == ("control_a", "control_b")
        assert a.fit.payload.scales[1][1] == 1
        missing = impact(replace(supplied, target_features=None), ModelType.MATCHED_CONTROLS)
        assert missing.fit.status is FitStatus.MISSING_INPUT
        assert missing.fit.payload.exclusions[0][1] == "MISSING_PRIOR_TARGET_FEATURES"
        empty = impact(replace(supplied, controls=()), ModelType.MATCHED_CONTROLS)
        assert empty.fit.status is FitStatus.INSUFFICIENT
        assert empty.fit.payload.selected == ()
        assert all(c.value is None for c in empty.cars)
    finally:
        store.close()


def codec_bytes(result):
    from eie.persistence import codec

    return codec.dumps(result)


def test_control_post_window_missing_data_does_not_change_matching(tmp_path):
    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        good = impact(supplied, ModelType.MATCHED_CONTROLS, matching=MatchingPolicy(k=2))
        incomplete = replace(
            supplied.controls[0],
            market=replace(
                supplied.controls[0].market,
                bars=tuple(
                    b
                    for b in supplied.controls[0].market.bars
                    if b.end_ns <= supplied.study.anchor_ns
                ),
            ),
        )
        r = impact(
            replace(supplied, controls=(incomplete, *supplied.controls[1:])),
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2),
        )
        assert r.fit.payload == good.fit.payload
        assert r.cars[0].value is None
        poor = impact(
            supplied,
            ModelType.MATCHED_CONTROLS,
            matching=MatchingPolicy(k=2, maximum_distance=0.001),
        )
        assert ImpactQuality.POOR_CONTROL_MATCH in poor.quality
        bad_target = replace(supplied.target_features, values=(("beta", 1e308), ("size", 1e308)))
        extreme = impact(
            replace(supplied, target_features=bad_target, controls=supplied.controls[:1]),
            ModelType.MATCHED_CONTROLS,
        )
        assert extreme.fit.status is FitStatus.NUMERIC_UNAVAILABLE
        extremes = tuple(
            replace(c, features=replace(c.features, values=(("beta", v), ("size", v))))
            for c, v in zip(supplied.controls[:2], (1.7e308, -1.7e308), strict=True)
        )
        overflowing_scale = impact(replace(supplied, controls=extremes), ModelType.MATCHED_CONTROLS)
        assert overflowing_scale.fit.status is FitStatus.NUMERIC_UNAVAILABLE
    finally:
        store.close()


def test_dataset_pin_ties_are_deterministic_across_hash_seeds(tmp_path):
    import os
    import subprocess
    import sys

    from eie.persistence import codec

    store, _, (_, _, supplied) = inputs(tmp_path)
    try:
        target_candidate = ControlCandidate(
            supplied.target.identity.instrument_id,
            supplied.target,
            feature(supplied.target.identity.instrument_id, supplied.study.anchor_ns, 20),
        )
        supplied = replace(
            supplied,
            controls=tuple(
                sorted((*supplied.controls, target_candidate), key=lambda c: c.instrument_id)
            ),
        )
        path = tmp_path / "inputs.json"
        path.write_bytes(codec.dumps(supplied))
        script = (
            "from pathlib import Path; import sys; from eie.persistence import codec; "
            "from eie.impact.engine import ImpactEngine; from eie.impact.models import ModelType; "
            "from examples.counterfactual_impact import ESTIMATION,CARS; "
            "r=ImpactEngine().impact_event(codec.loads(Path(sys.argv[1]).read_bytes()), "
            "ModelType.MATCHED_CONTROLS,ESTIMATION,CARS); "
            "print(r.model_spec.model_id,r.result_fingerprint)"
        )
        outputs = tuple(
            subprocess.check_output(
                [sys.executable, "-c", script, str(path)],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
            )
            for seed in ("1", "23")
        )
        assert outputs[0] == outputs[1]
    finally:
        store.close()
