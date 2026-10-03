from dataclasses import FrozenInstanceError, replace

import pytest
from analogue_helpers import FP, NUMERIC, POLICY, SPEC, TYPE, context, inputs, response, row

from eie.analogues.models import (
    AnalogueLimits,
    AnalogueRunManifest,
    AnalogueUncertainty,
    DistanceComponent,
    FeatureValue,
    NumericScale,
    PoolMember,
)
from eie.analogues.reaction_surprise import compare
from eie.analogues.responses import distribution
from eie.analogues.retrieval import retrieve
from eie.analogues.schema import (
    FeatureKind,
    ResponseMetric,
    TypeCondition,
    UncertaintyKind,
    UncertaintyState,
)
from eie.persistence.codec import dumps, loads


@pytest.mark.parametrize(
    "kind",
    [
        "row",
        "context",
        "policy",
        "spec",
        "response",
        "query",
        "result",
        "reaction",
        "run",
        "scale",
        "distribution",
    ],
)
def test_immutable_allowlisted_roundtrip(kind):
    data, q = inputs()
    result = retrieve(data, q, POLICY)
    values = {
        "row": data.target,
        "context": context(),
        "policy": POLICY,
        "spec": SPEC,
        "response": data.responses[0],
        "query": q,
        "result": result,
        "reaction": compare(result, response(data.target), q.available_ns + 2),
        "run": AnalogueRunManifest((result.result_id,), (result.fingerprint,), "dataset", 1000),
        "scale": result.scales[0],
        "distribution": result.distribution,
    }
    value = values[kind]
    assert loads(dumps(value)) == value and dumps(loads(dumps(value))) == dumps(value)
    with pytest.raises((FrozenInstanceError, TypeError)):
        value.available_ns = 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("weight", 0),
        ("weight", -1),
        ("weight", float("inf")),
        ("weight", True),
        ("missing_penalty", -1),
        ("missing_penalty", 1.1),
        ("kind", "NUMERIC"),
        ("name", ""),
        ("name", " x "),
        ("name", "x\n"),
        ("name", "x" * 257),
        ("ordinal_labels", ("LOW", "HIGH")),
    ],
)
def test_feature_contract_rejects_unsafe_or_ambiguous_comparisons(field, value):
    with pytest.raises(ValueError):
        replace(NUMERIC, **{field: value})


@pytest.mark.parametrize("labels", [(), ("ONLY",), ("LOW", "LOW"), (" LOW", "HIGH")])
def test_ordinal_order_must_be_explicit_and_distinct(labels):
    with pytest.raises(ValueError):
        replace(TYPE, kind=FeatureKind.ORDINAL, ordinal_labels=labels)


@pytest.mark.parametrize(
    "field,value",
    [
        ("features", ()),
        ("features", (TYPE, TYPE)),
        ("allowed_types", ("X",)),
        ("type_condition", TypeCondition.ALLOWED_SET),
        ("minimum_weight_fraction", 0),
        ("minimum_weight_fraction", 1.1),
        ("minimum_comparable_features", 3),
        ("minimum_history", 0),
        ("minimum_scale_samples", 1),
        ("maximum_distance", -1),
        ("poor_distance_threshold", -1),
        ("stability_threshold", -1),
        ("blocking_dimensions", ("a", "a")),
    ],
)
def test_policy_bounds_and_identity_change(field, value):
    with pytest.raises(ValueError):
        replace(POLICY, **{field: value})
    assert replace(POLICY, version="2").policy_id != POLICY.policy_id


@pytest.mark.parametrize("field", list(AnalogueLimits.__dataclass_fields__))
@pytest.mark.parametrize("value", [0, -1, 100000001, True])
def test_all_operational_bounds_fail_closed(field, value):
    with pytest.raises(ValueError):
        replace(AnalogueLimits(), **{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observed_ns": 11},
        {"valid_from_ns": 12, "valid_to_ns": 11},
        {"value": "bad\nlabel"},
        {"source": ""},
        {"value": float("nan")},
    ],
)
def test_context_labels_are_inert_bounded_and_clocks_consistent(kwargs):
    with pytest.raises(ValueError):
        replace(context(), **kwargs)


def test_context_independent_valid_and_known_times():
    c = context(valid_from_ns=5, valid_to_ns=15)
    assert c.visible(10, 10) and not c.visible(9, 10) and not c.visible(10, 15)
    assert not c.visible(10, 4)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revision": 0},
        {"event_fingerprint": "bad"},
        {"feature_cutoff_ns": 11},
        {"available_ns": 9},
        {"values": ()},
        {"contexts": (context(cutoff=11),)},
    ],
)
def test_feature_row_validates_lineage_and_own_exante_cutoff(kwargs):
    with pytest.raises(ValueError):
        replace(row(), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": None},
        {"reason": "MISSING"},
        {"input_ids": ("x", "x")},
        {"value": " x"},
        {"signature": ""},
    ],
)
def test_feature_missingness_requires_reason(kwargs):
    with pytest.raises(ValueError):
        replace(FeatureValue("x", 1.0, "S", ()), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"study_cohort": "bad"},
        {"offset_ns": -1},
        {"model_cohort": FP},
        {"metric": ResponseMetric.ABNORMAL_RETURN},
        {"metric": ResponseMetric.CAR},
        {"car_label": "post"},
    ],
)
def test_response_spec_compatibility_is_explicit(kwargs):
    with pytest.raises(ValueError):
        replace(SPEC, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revision": 0},
        {"available_ns": 9},
        {"value": None},
        {"reason": "MISSING"},
        {"impact_id": "impact"},
        {"impact_fingerprint": FP},
        {"study_fingerprint": "bad"},
        {"quality": ("Z", "A")},
    ],
)
def test_response_observation_requires_exact_provenance_and_missingness(kwargs):
    with pytest.raises(ValueError):
        replace(response(row()), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 0},
        {"top_k": 1001},
        {"target_revision": 0},
        {"available_ns": 999},
        {"earliest_ns": 1001},
        {"schema_version": "2.0"},
    ],
)
def test_query_contract_versions_and_bounded_selection(kwargs):
    _, q = inputs()
    with pytest.raises(ValueError):
        replace(q, **kwargs)


@pytest.mark.parametrize(
    "kwargs", [{"count": -1}, {"value": -1}, {"value": None}, {"member_fingerprint": "bad"}]
)
def test_scale_references_are_nonnegative_and_explicit(kwargs):
    from eie.analogues.schema import ScaleMethod

    s = NumericScale("x", ScaleMethod.MAD, 5, 1.0, None, FP)
    with pytest.raises(ValueError):
        replace(s, **kwargs)


@pytest.mark.parametrize("kwargs", [{"weight": 0}, {"distance": -1}, {"penalty": -1}])
def test_distance_component_rejects_invalid_math(kwargs):
    d = DistanceComponent("x", FeatureKind.NUMERIC, 1.0, 2.0, 1, 1, 1, 0, "RULE", None)
    with pytest.raises(ValueError):
        replace(d, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revision": 0},
        {"total_distance": -1},
        {"numerator": -1},
        {"total_weight": 0},
        {"comparable_weight": 0},
        {"comparable_count": 0},
        {"missing_penalty": -1},
    ],
)
def test_candidate_has_comparable_weight_and_transparent_numerator(kwargs):
    data, q = inputs()
    candidate = retrieve(data, q, POLICY).candidates[0]
    with pytest.raises(ValueError):
        replace(candidate, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"response_id": "response"}, {"response_fingerprint": FP}, {"row_fingerprint": "bad"}],
)
def test_pool_members_pin_complete_references(kwargs):
    with pytest.raises(ValueError):
        replace(PoolMember("row", FP, None, None, None), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_ids": ("a", "a")},
        {"candidate_ids": ()},
        {"raw_values": (float("nan"),)},
    ],
)
def test_distribution_membership_is_exact_and_finite(kwargs):
    d = distribution(SPEC, (("a", 1.0), ("b", 2.0)), 1)
    with pytest.raises(ValueError):
        replace(d, **kwargs)


def test_uncertainty_reasons_are_not_optional_or_unordered():
    with pytest.raises(ValueError):
        AnalogueUncertainty(UncertaintyKind.POOL_SIZE, UncertaintyState.LIMITED, ())
    with pytest.raises(ValueError):
        AnalogueUncertainty(
            UncertaintyKind.POOL_SIZE, UncertaintyState.LIMITED, ("A",), (("z", 1), ("a", 1))
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"uncertainty": ()},
        {"schema_version": "2"},
        {"excluded_event_ids": ("other",)},
        {"target_fingerprint": "bad"},
    ],
)
def test_result_requires_all_dimensions_and_current_exclusion(kwargs):
    data, q = inputs()
    with pytest.raises(ValueError):
        replace(retrieve(data, q, POLICY), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"signed_percentile": -0.1},
        {"magnitude_percentile": 1.1},
        {"tie_rule": "UPPER"},
        {"mode": "EX_ANTE"},
        {"target_response_id": None},
    ],
)
def test_reaction_is_distinct_and_expost(kwargs):
    data, q = inputs()
    s = compare(retrieve(data, q, POLICY), response(data.target), q.available_ns + 2)
    with pytest.raises(ValueError):
        replace(s, **kwargs)


def test_run_membership_fingerprints_are_complete():
    with pytest.raises(ValueError):
        AnalogueRunManifest(("result",), (), "dataset", 1)
    with pytest.raises(ValueError):
        AnalogueRunManifest(("result",), ("bad",), "dataset", 1)
