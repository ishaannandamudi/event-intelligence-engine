from dataclasses import replace

import pytest
from analogue_helpers import POLICY, TYPE, inputs, response

from eie.analogues.pool import unique_rows
from eie.analogues.retrieval import retrieve
from eie.analogues.schema import ContaminationPolicy, Exclusion, TypeCondition


@pytest.mark.parametrize("clock", [999, 1000, 1001])
def test_strict_prior_response_completion(clock):
    data, q = inputs()
    r = replace(data.responses[0], completed_ns=clock, available_ns=clock)
    result = retrieve(replace(data, responses=(r, *data.responses[1:])), q, POLICY)
    selected = {c.row_id for c in result.candidates}
    assert (r.feature_row_id in selected) == (clock < q.known_as_of_ns)


@pytest.mark.parametrize("condition", list(TypeCondition))
def test_explicit_type_conditioning(condition):
    p = replace(
        POLICY,
        features=(TYPE,),
        type_condition=condition,
        allowed_types=("EARNINGS.OTHER",) if condition is TypeCondition.ALLOWED_SET else (),
    )
    data, q = inputs(policy=p)
    r = replace(data.rows[0], event_type="EARNINGS.OTHER")
    rs = response(r)
    result = retrieve(
        replace(data, rows=(r, *data.rows[1:]), responses=(rs, *data.responses[1:])), q, p
    )
    ids = {c.event_id for c in result.candidates}
    assert (r.event_id in ids) == (condition is not TypeCondition.EXACT)
    if condition is TypeCondition.ALLOWED_SET:
        assert ids == {r.event_id}


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"event_available_ns": 1000, "feature_cutoff_ns": 1000, "available_ns": 1000},
            Exclusion.NOT_PRIOR,
        ),
        (
            {"event_available_ns": 1100, "feature_cutoff_ns": 1100, "available_ns": 1100},
            Exclusion.NOT_PRIOR,
        ),
        ({"available_ns": 1000}, Exclusion.NOT_PRIOR),
        ({"available_ns": 1001}, Exclusion.NOT_PRIOR),
        ({"reconstruction_reasons": ("GAP",)}, Exclusion.RECONSTRUCTION_LIMITATION),
    ],
)
def test_future_same_time_and_qualified_history(change, reason):
    p = replace(POLICY, reject_reconstruction=True)
    data, q = inputs(policy=p)
    r = replace(data.rows[0], **change)
    result = retrieve(
        replace(data, rows=(r, *data.rows[1:]), responses=(response(r), *data.responses[1:])), q, p
    )
    assert next(m.exclusion for m in result.pool if m.row_id == r.row_id) is reason


def test_current_event_all_aliases_and_revisions_excluded():
    data, q = inputs()
    alias = replace(
        data.rows[0], alias_event_ids=tuple(sorted((data.rows[0].event_id, data.target.event_id)))
    )
    current = data.target
    revised = replace(current, revision=2, event_revision_id="revision-2")
    result = retrieve(
        replace(
            data,
            rows=tuple(sorted((*data.rows[1:], alias, current, revised), key=lambda r: r.row_id)),
            responses=(*data.responses[1:], response(alias), response(current), response(revised)),
        ),
        q,
        POLICY,
    )
    assert {alias.row_id, current.row_id, revised.row_id}.isdisjoint(
        c.row_id for c in result.candidates
    )
    assert dict(result.diagnostics.excluded)[Exclusion.SAME_EVENT] == 3


@pytest.mark.parametrize("policy", list(ContaminationPolicy))
@pytest.mark.parametrize("cohort", [False, True])
def test_contamination_has_explicit_policy_and_diagnostics(policy, cohort):
    p = replace(POLICY, features=(TYPE,), contamination=policy, contaminated_cohort=cohort)
    data, q = inputs(policy=p)
    flagged = replace(data.responses[0], contaminated=True, quality=("CONTAMINATED",))
    result = retrieve(replace(data, responses=(flagged, *data.responses[1:])), q, p)
    selected = {c.row_id for c in result.candidates}
    assert (flagged.feature_row_id in selected) == (
        policy is ContaminationPolicy.INCLUDE_WITH_FLAG
        or policy is ContaminationPolicy.SEPARATE_COHORT
        and cohort
    )
    assert "CONTAMINATION_IN_HISTORY" in result.quality
    assert (
        next(u for u in result.uncertainty if u.kind.value == "CONTAMINATION").state.value
        == "LIMITED"
    )


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing", Exclusion.INSUFFICIENT_RESPONSE),
        ("value", Exclusion.INSUFFICIENT_RESPONSE),
        ("gap", Exclusion.DATA_GAP),
        ("coarse", Exclusion.COARSE_TIME),
        ("cohort", Exclusion.INCOMPATIBLE_RETURN_TYPE),
    ],
)
def test_incomplete_and_incompatible_responses(kind, reason):
    data, q = inputs()
    r = data.responses[0]
    if kind == "missing":
        responses = data.responses[1:]
    else:
        r = replace(
            r,
            **(
                {"value": None, "reason": "MISSING"}
                if kind == "value"
                else {"quality": ("DATA_GAP",)}
                if kind == "gap"
                else {"quality": ("COARSE_EVENT_TIME",)}
                if kind == "coarse"
                else {"spec": replace(r.spec, offset_ns=2)}
            ),
        )
        responses = (r, *data.responses[1:])
    result = retrieve(replace(data, responses=responses), q, POLICY)
    assert next(m.exclusion for m in result.pool if m.row_id == data.rows[0].row_id) is reason


def test_latest_completed_revision_is_one_event_and_aliases_one_sample():
    data, q = inputs()
    old = data.rows[0]
    new = replace(
        old,
        revision=2,
        event_revision_id="revision2",
        event_available_ns=80,
        feature_cutoff_ns=80,
        available_ns=80,
    )
    alias = replace(
        data.rows[1], alias_event_ids=tuple(sorted((old.event_id, data.rows[1].event_id)))
    )
    # Both alias rows carry the same historical EIE-11 group membership.
    new = replace(new, alias_event_ids=alias.alias_event_ids)
    rows = tuple(sorted((old, new, alias, *data.rows[2:]), key=lambda r: r.row_id))
    result = retrieve(
        replace(
            data,
            rows=rows,
            responses=(response(old), response(new), response(alias), *data.responses[2:]),
        ),
        q,
        POLICY,
    )
    assert new.row_id in {c.row_id for c in result.candidates}
    assert old.row_id not in {c.row_id for c in result.candidates}
    assert alias.row_id not in {c.row_id for c in result.candidates}


def test_ambiguous_same_revision_features_fail_closed():
    data, q = inputs()
    old = data.rows[0]
    revised = replace(old, values=(replace(old.values[0], value=99), old.values[1]))
    with pytest.raises(ValueError, match="ambiguous"):
        unique_rows(
            replace(data, rows=(old, revised), responses=(response(old), response(revised))),
            q,
            POLICY,
        )


def test_later_known_alias_with_lower_stable_id_does_not_duplicate_old_event():
    data, q = inputs()
    old = data.rows[-1]
    later_alias = replace(
        data.rows[0],
        event_available_ns=80,
        feature_cutoff_ns=80,
        available_ns=80,
        alias_event_ids=tuple(sorted((old.event_id, data.rows[0].event_id))),
    )
    rows = (later_alias, *data.rows[1:])
    data = replace(data, rows=rows, responses=tuple(response(r) for r in rows))
    result = retrieve(data, q, POLICY)
    assert len(result.candidates) == 6
    assert later_alias.row_id in {c.row_id for c in result.candidates}
    assert old.row_id not in {c.row_id for c in result.candidates}


def test_pinned_current_eie11_split_overrides_old_feature_alias_memberships():
    data, q = inputs()
    first, second = data.rows[:2]
    aliases = tuple(sorted((first.event_id, second.event_id)))
    rows = (
        replace(first, alias_event_ids=aliases),
        replace(second, alias_event_ids=aliases),
        *data.rows[2:],
    )
    data = replace(
        data,
        rows=rows,
        responses=tuple(response(r) for r in rows),
        alias_groups=tuple((r.event_id,) for r in rows),
    )
    result = retrieve(data, q, POLICY)
    assert len(result.candidates) == 7


@pytest.mark.parametrize("clock,expected", [(80, 5), (1000, 6), (1001, 6)])
def test_alias_evidence_without_response_only_applies_when_strictly_prior(clock, expected):
    data, q = inputs()
    old, other, link = data.rows[-1], data.rows[0], data.rows[2]
    link = replace(
        link,
        event_available_ns=clock,
        feature_cutoff_ns=clock,
        available_ns=clock,
        alias_event_ids=tuple(sorted((link.event_id, old.event_id, other.event_id))),
    )
    rows = (data.rows[0], data.rows[1], link, *data.rows[3:])
    data = replace(data, rows=rows, responses=tuple(response(r) for r in rows if r != link))
    assert len(retrieve(data, q, POLICY).candidates) == expected
