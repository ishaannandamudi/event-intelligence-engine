from dataclasses import FrozenInstanceError, replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.impact.inputs import (
    ControlCandidate,
    FitExclusion,
)
from eie.impact.models import (
    CARWindow,
    ContaminationPolicy,
    FitMethod,
    FitStatus,
    MatchingPolicy,
    ModelType,
    UncertaintyInterval,
)
from eie.impact.sensitivity import EventImpactSet, SensitivityResult, impact_set
from eie.persistence import codec
from eie.studies.market import Adjustment, ReturnType
from eie.studies.models import SessionPolicy
from examples.counterfactual_impact import ESTIMATION, factor_inputs
from examples.event_studies import MINUTE
from tests.impact_helpers import impact, inputs


@pytest.fixture(scope="module")
def contracts(tmp_path_factory):
    store, reader, (base, sector, controls) = inputs(tmp_path_factory.mktemp("impact-contracts"))
    yield base, impact(base), impact(sector), controls, reader
    store.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"start_offset": 0},
        {"end_offset": 0},
        {"start_offset": -1},
        {"start_offset": -(2**63)},
        {"gap_ns": -1},
        {"end_offset": True},
        {"session_policy": "ELAPSED"},
    ],
)
def test_invalid_estimation_contracts(fields):
    with pytest.raises(ValidationError):
        replace(ESTIMATION, **fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"minimum_observations": 1},
        {"minimum_observations": 100001},
        {"minimum_uncertainty_observations": 29},
        {"minimum_uncertainty_observations": 100001},
        {"confidence_level": 0},
        {"confidence_level": 1},
        {"confidence_level": float("nan")},
        {"low_r2_threshold": -1},
        {"maximum_condition": 1},
        {"intercept": 1},
        {"schema_version": "2.0"},
        {"study_fingerprint": "bad"},
        {"input_fingerprint": "bad"},
        {"event_dataset_fingerprint": "bad"},
        {"fit_method": FitMethod.IDENTITY_BENCHMARK},
        {"matching": MatchingPolicy()},
        {"universe": ("z", "a")},
        {"datasets": ()},
        {"car_windows": ()},
        {"model_version": " "},
        {"fit_cutoff_ns": -1},
    ],
)
def test_model_specs_fail_closed_or_pin_rejected_by_calculation(contracts, fields):
    from eie.impact.engine import calculate

    _, result, _, _, _ = contracts
    with pytest.raises(ValidationError):
        spec = replace(result.model_spec, **fields)
        calculate(result.inputs, spec)


def test_estimation_gap_clock_frequency_and_car_boundaries(contracts):
    _, r, _, _, _ = contracts
    spec = r.model_spec
    for window in (
        replace(ESTIMATION, end_offset=-3 * MINUTE, gap_ns=0),
        replace(ESTIMATION, gap_ns=17 * MINUTE),
        replace(ESTIMATION, session_policy=SessionPolicy.TRADING),
    ):
        with pytest.raises(ValidationError):
            replace(spec, estimation_window=window)
    with pytest.raises(ValidationError):
        replace(spec, car_windows=(CARWindow("outside", 0, 6 * MINUTE),))
    with pytest.raises(ValidationError):
        replace(spec, car_windows=(spec.car_windows[0],) * 101)
    with pytest.raises(ValidationError):
        replace(spec, datasets=tuple(reversed(spec.datasets)))
    with pytest.raises(ValidationError):
        replace(spec, car_windows=tuple(reversed(spec.car_windows)))
    with pytest.raises(ValidationError):
        replace(spec, car_windows=(spec.car_windows[0], spec.car_windows[0]))
    with pytest.raises(ValidationError):
        CARWindow("empty", 1, 1)
    with pytest.raises(ValidationError):
        CARWindow("bad", 0, 1, "COMPOUND_UNDECLARED")


@pytest.mark.parametrize(
    "fields",
    [
        {"k": 0},
        {"k": 101},
        {"minimum_scale": 0},
        {"maximum_distance": 0},
        {"minimum_prior_periods": 0},
        {"weighting": "EQUAL"},
    ],
)
def test_matching_policy_bounds(fields):
    with pytest.raises(ValidationError):
        MatchingPolicy(**fields)


def test_models_immutable_identity_changes_and_canonical_codec(contracts):
    base, r, sector, supplied, _ = contracts
    with pytest.raises(FrozenInstanceError):
        r.model_spec.model_version = "mutated"
    spec = r.model_spec
    for other in (
        replace(spec, model_version="version-2"),
        replace(spec, estimation_window=replace(ESTIMATION, start_offset=-200 * MINUTE)),
        replace(spec, contamination_policy=ContaminationPolicy.EXCLUDE_EVENT),
        replace(spec, intercept=False),
    ):
        assert other.model_id != spec.model_id
    assert sector.model_spec.model_id != spec.model_id
    controls = impact(supplied, ModelType.MATCHED_CONTROLS)
    changed = impact(replace(supplied, controls=supplied.controls[:2]), ModelType.MATCHED_CONTROLS)
    assert controls.model_spec.model_id != changed.model_spec.model_id
    for v in (r, spec, r.fit, impact_set((r, sector)), factor_inputs(base).factors[0]):
        assert codec.dumps(codec.loads(codec.dumps(v))) == codec.dumps(v)
    with pytest.raises(PersistenceError):
        codec.loads(b'{"type":"os.system","fields":{}}')


def test_factor_and_feature_input_validation(contracts):
    base, r, _, supplied, _ = contracts
    f = factor_inputs(base).factors[0]
    for fields in (
        {"dataset_fingerprint": "bad"},
        {"factor_id": "INTERCEPT"},
        {"observations": tuple(reversed(f.observations))},
        {"observations": (f.observations[0], f.observations[0])},
        {"observations": (f.observations[0],) * 100001},
    ):
        with pytest.raises(ValidationError):
            replace(f, **fields)
    for fields in (
        {"start_ns": f.observations[0].end_ns},
        {"available_ns": 0},
        {"value": float("inf")},
    ):
        with pytest.raises(ValidationError):
            replace(f.observations[0], **fields)
    with pytest.raises(ValidationError):
        replace(f.semantics, period_definition="OPEN_TO_CLOSE")
    with pytest.raises(ValidationError):
        replace(
            f.semantics, return_type=ReturnType.TOTAL_RETURN, adjustment=Adjustment.SPLIT_ADJUSTED
        )
    feature = supplied.target_features
    for fields in (
        {"values": ()},
        {"values": (("z", 1.0), ("a", 2.0))},
        {"values": (("a", 1.0), ("a", 2.0))},
        {"values": tuple((str(j), 1.0) for j in range(17))},
        {"available_ns": 0},
        {"dataset_fingerprint": "bad"},
        {"values": ((" ", 1.0),)},
    ):
        with pytest.raises(ValidationError):
            replace(feature, **fields)
    with pytest.raises(ValidationError):
        FitExclusion("instrument", 2, 1, 0, "event", "source")
    with pytest.raises(ValidationError):
        ControlCandidate("other", base.target, None)
    with pytest.raises(ValidationError):
        ControlCandidate("other", None, feature)
    with pytest.raises(ValidationError):
        ControlCandidate("other", None, None, (base.study.contamination[0],) * 2)
    flag = base.study.contamination[0]
    with pytest.raises(ValidationError):
        ControlCandidate(
            "other",
            None,
            None,
            tuple(
                sorted(
                    (flag, replace(flag, reasons=("OTHER_REASON",))),
                    key=lambda c: (c.timestamp_ns, c.event_id, c.revision_id),
                )
            ),
        )
    for fields in (
        {"target_features": replace(feature, instrument_id="other")},
        {"controls": supplied.controls * 1001},
        {"factors": (f,) * 9},
        {"factors": (f, f)},
        {"fit_exclusions": (FitExclusion("i", 1, 2, 0, "e", "s"),) * 2},
        {
            "target": replace(
                base.target, identity=replace(base.target.identity, dataset_id="changed")
            )
        },
    ):
        with pytest.raises(ValidationError):
            replace(base, **fields)
    with pytest.raises(ValidationError):
        replace(
            r.model_spec,
            semantics=replace(r.model_spec.semantics, timeframe=base.target.identity.timeframe.H1),
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"confidence_level": 1},
        {"standard_error": -1},
        {"lower": 2},
    ],
)
def test_uncertainty_contract_validation(fields):
    with pytest.raises(ValidationError):
        replace(UncertaintyInterval(0.95, 0.1, 0, 1), **fields)


def test_derived_contract_missingness_order_and_shape_validation(contracts):
    _, r, other, supplied, _ = contracts
    with pytest.raises(ValidationError):
        replace(r.fit, status=FitStatus.INSUFFICIENT)
    with pytest.raises(ValidationError):
        replace(r.fit, observation_count=-1)
    with pytest.raises(ValidationError):
        replace(r.fit, reasons=("z", "a"))
    with pytest.raises(ValidationError):
        replace(r.fit, period_fingerprints=("bad",))
    with pytest.raises(ValidationError):
        replace(r.fit, payload=None)
    with pytest.raises(ValidationError):
        replace(r.fit.payload, covariance=())
    with pytest.raises(ValidationError):
        replace(r.fit.payload, standard_errors=(-1.0, 1.0))
    with pytest.raises(ValidationError):
        replace(r.abnormal_curve[0], observed_return=None)
    with pytest.raises(ValidationError):
        replace(r.abnormal_curve[0], start_ns=r.abnormal_curve[0].end_ns)
    with pytest.raises(ValidationError):
        replace(r.cars[0], period_count=-1)
    with pytest.raises(ValidationError):
        replace(r, abnormal_curve=tuple(reversed(r.abnormal_curve)))
    with pytest.raises(ValidationError):
        replace(r, available_ns=r.inputs.study.post_end_ns - 1)
    with pytest.raises(ValidationError):
        replace(r, model_spec=replace(r.model_spec, input_fingerprint="b" * 64))
    with pytest.raises(ValidationError):
        SensitivityResult("b", "a", "post", 0, 0, 0, ())
    with pytest.raises(ValidationError):
        EventImpactSet((r,), ())
    from eie.impact.engine import run_manifest

    run = run_manifest((r, other))
    for fields in (
        {"impact_ids": ()},
        {"fit_failure_count": 3},
        {"event_dataset_fingerprint": "bad"},
        {"result_fingerprints": ("bad", "bad")},
        {"schema_version": "2.0"},
    ):
        with pytest.raises(ValidationError):
            replace(run, **fields)
    c = impact(supplied, ModelType.MATCHED_CONTROLS).fit.payload
    for fields in (
        {"scales": (("size", 0),)},
        {"selected": (c.selected[0],) * 2},
        {"exclusions": (("z", "z"), ("a", "a"))},
        {"scaling_members": ("z", "a")},
    ):
        with pytest.raises(ValidationError):
            replace(c, **fields)
    with pytest.raises(ValidationError):
        replace(c.selected[0], weight=2)
