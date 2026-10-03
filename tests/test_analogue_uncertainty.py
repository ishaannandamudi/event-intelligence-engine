from dataclasses import replace

from analogue_helpers import NUMERIC, POLICY, TYPE, context, inputs

from eie.analogues.retrieval import retrieve
from eie.analogues.schema import FeatureSource, MissingPolicy, UncertaintyKind


def by_kind(result):
    return {u.kind.value: u for u in result.uncertainty}


def test_all_eight_uncertainties_are_separate_with_visible_diagnostics():
    data, q = inputs()
    result = retrieve(data, q, POLICY)
    assert {u.kind for u in result.uncertainty} == set(UncertaintyKind)
    assert all(u.reasons for u in result.uncertainty)
    assert not hasattr(result, "confidence")


def test_similar_nearest_distance_different_response_reconstruction_diagnostics():
    data, q = inputs()
    good = retrieve(data, q, POLICY)
    row = replace(data.rows[0], reconstruction_reasons=("SYNTHETIC_RECONSTRUCTION",))
    poor_data = replace(data, rows=(row, *data.rows[1:]), responses=data.responses[1:])
    poor = retrieve(poor_data, q, POLICY)
    assert poor.diagnostics.nearest_distance == good.diagnostics.nearest_distance == 0
    assert by_kind(poor)["RECONSTRUCTION"] != by_kind(good)["RECONSTRUCTION"]
    assert by_kind(poor)["RESPONSE_COVERAGE"] != by_kind(good)["RESPONSE_COVERAGE"]


def test_sparse_context_and_optional_feature_coverage_are_not_full_confidence():
    f = replace(
        NUMERIC,
        source=FeatureSource.CONTEXT,
        selector="number",
        missing=MissingPolicy.IGNORE_WITH_PENALTY,
    )
    p = replace(POLICY, features=(f, TYPE))
    data, q = inputs(policy=p, values=(None,) * 7)
    # Mathematical input feature missingness accompanies explicit context absence.
    target = replace(data.target, contexts=(context(cutoff=1000),))
    result = retrieve(replace(data, target=target), q, p)
    u = by_kind(result)
    assert u["CONTEXT_COVERAGE"].state.value == "LIMITED"
    assert u["FEATURE_COVERAGE"].state.value == "LIMITED"


def test_tiny_pool_and_all_far_history_are_visible():
    data, q = inputs(count=2)
    tiny = retrieve(data, q, POLICY)
    assert by_kind(tiny)["POOL_SIZE"].state.value == "LIMITED"
    data, q = inputs(target_value=100)
    far = retrieve(data, q, POLICY)
    assert by_kind(far)["DISTANCE_QUALITY"].state.value == "LIMITED"
    assert not far.candidates


def test_nested_subset_instability_is_descriptive():
    data, q = inputs(
        count=20, values=(1.0,) * 20, target_value=1, response_values=(0.0,) * 5 + (1.0,) * 15
    )
    result = retrieve(data, q, POLICY)
    assert result.diagnostics.stability_medians == ((5, 0.0), (10, 0.5), (20, 1.0))
    assert by_kind(result)["DISTRIBUTION_STABILITY"].state.value == "LIMITED"
    data, q = inputs(count=20, values=(1.0,) * 20, target_value=1, response_values=(0.0,) * 20)
    assert by_kind(retrieve(data, q, POLICY))["DISTRIBUTION_STABILITY"].state.value == "OBSERVED"


def test_finite_extremes_do_not_produce_infinite_stability_diagnostics():
    data, q = inputs(
        count=20, values=(1.0,) * 20, target_value=1, response_values=(-1e308,) * 5 + (1e308,) * 15
    )
    result = retrieve(data, q, POLICY)
    component = by_kind(result)["DISTRIBUTION_STABILITY"]
    assert component.state.value == "UNAVAILABLE"
    assert component.reasons == ("NESTED_MEDIAN_NUMERIC_OVERFLOW",)
    assert dict(component.diagnostics)["median_range"] is None


def test_response_reconstruction_is_preserved_and_can_be_explicitly_excluded():
    from eie.analogues.schema import Exclusion

    data, q = inputs()
    response = replace(data.responses[0], reconstruction_reasons=("DATE_ONLY_RECONSTRUCTED",))
    data = replace(data, responses=(response, *data.responses[1:]))
    result = retrieve(data, q, POLICY)
    assert by_kind(result)["RECONSTRUCTION"].state.value == "LIMITED"
    policy = replace(POLICY, reject_reconstruction=True)
    q = replace(q, policy_id=policy.policy_id)
    data = replace(
        data,
        target=replace(data.target, policy_id=policy.policy_id),
        rows=tuple(replace(r, policy_id=policy.policy_id) for r in data.rows),
    )
    q = replace(q, target_row_id=data.target.row_id)
    # Response references change when their feature policy creates new immutable rows.
    data = replace(
        data,
        responses=tuple(
            replace(response, feature_row_id=row.row_id)
            for response, row in zip(data.responses, data.rows, strict=True)
        ),
    )
    excluded = retrieve(data, q, policy)
    member = next(m for m in excluded.pool if m.row_id == data.rows[0].row_id)
    assert member.exclusion is Exclusion.RECONSTRUCTION_LIMITATION
