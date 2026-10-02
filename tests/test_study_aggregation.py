from dataclasses import fields, is_dataclass, replace

import pytest

from eie.core.errors import ValidationError
from eie.expectations.models import EPS_DILUTED, MeasurementContext
from eie.studies.aggregation import (
    DistributionQuery,
    cohort,
    historical_distribution,
    quantile,
    selected,
)
from eie.studies.decay import InfluencePoint, class_decay, decay, summarize
from eie.studies.models import ContextLabel, ReturnMethod, StudyQuality
from eie.studies.numerics import median
from eie.studies.queries import EventStudyFilter
from examples.event_studies import EARNINGS, MINUTE, history
from tests.expectations_helpers import historical_service
from tests.study_helpers import result


@pytest.fixture
def studies(tmp_path):
    store, reader, ids = history(tmp_path)
    values = tuple(result(reader, i, d) for d, i in zip((3, 5, 7, 9), ids, strict=True))
    yield reader, values
    store.close()


def query(values, **changes):
    last = values[-1]
    q = DistributionQuery(
        last.anchor_ns,
        last.definition.event_build_id,
        EARNINGS.code,
        cohort(last),
        2 * MINUTE,
        2,
        exclude_event_id=last.definition.event.event_id,
    )
    return replace(q, **changes)


def test_distribution_statistics_are_strictly_prior(studies):
    _, values = studies
    q = query(values)
    d = historical_distribution(values, q)
    assert d.count == 3 and d.mean is not None and d.median is not None and d.mad is not None
    assert d.positive_frequency == 1 and d.negative_frequency == 0
    assert len(d.quantiles) == 5 and d.median_absolute_response == d.median
    assert d.median_volatility_change is not None
    assert d == historical_distribution(tuple(reversed(values)), q)
    assert d.result_fingerprint == historical_distribution(values[:3], q).result_fingerprint
    future = replace(values[-1], post_return=10)
    assert d == historical_distribution((*values[:3], future), q)
    assert historical_distribution(values, query(values, minimum_samples=10)).reasons == (
        "INSUFFICIENT_SAMPLES",
        "STUDIES_WITH_QUALITY_WARNINGS",
    )
    assert (
        historical_distribution(
            values, query(values, as_of_ns=values[0].available_ns, exclude_event_id=None)
        ).count
        == 0
    )
    assert (
        historical_distribution(
            values,
            query(
                values,
                as_of_ns=values[0].available_ns + 1,
                exclude_event_id=None,
                minimum_samples=1,
            ),
        ).count
        == 1
    )


def test_current_event_is_excluded_even_after_completion(studies):
    _, values = studies
    q = query(values, as_of_ns=values[-1].available_ns + 1)
    assert historical_distribution(values, q).count == 3
    assert historical_distribution(values, replace(q, exclude_event_id=None)).count == 4


@pytest.mark.parametrize("change", ["method", "type", "build", "policy", "revision_duplicate"])
def test_incompatible_cohorts_and_duplicate_events_fail_closed(studies, change):
    _, values = studies
    r = values[0]
    if change == "method":
        r = replace(
            r,
            definition=replace(
                r.definition, policy=replace(r.definition.policy, return_method=ReturnMethod.LOG)
            ),
        )
    elif change == "policy":
        r = replace(
            r, definition=replace(r.definition, policy=replace(r.definition.policy, max_lag_ns=1))
        )
    elif change == "type":
        from eie.studies.market import Adjustment, ReturnType

        r = replace(
            r,
            market=replace(
                r.market,
                identity=replace(
                    r.market.identity,
                    adjustment=Adjustment.SPLIT_DIVIDEND_ADJUSTED,
                    return_type=ReturnType.TOTAL_RETURN,
                ),
            ),
        )
    elif change == "build":
        r = replace(r, definition=replace(r.definition, event_build_id="different"))
        assert historical_distribution((r, *values[1:]), query(values)).count == 2
        return
    else:
        with pytest.raises(ValidationError):
            selected((*values, r), query(values))
        return
    with pytest.raises(ValidationError):
        historical_distribution((r, *values[1:]), query(values))


def test_explicit_context_issuer_contamination_and_missing_responses(studies):
    _, values = studies
    labelled = tuple(
        replace(
            r,
            definition=replace(
                r.definition, context=(ContextLabel("regime", "calm", "S", "1", 0, 0),)
            ),
        )
        for r in values
    )
    q = query(labelled, context=(("regime", "calm"),))
    assert historical_distribution(labelled, q).count == 3
    assert historical_distribution(labelled, replace(q, context=(("regime", "stress"),))).count == 0
    assert historical_distribution(labelled, replace(q, issuer_id="other")).count == 0
    assert historical_distribution(labelled, replace(q, issuer_id="issuer_fixture")).count == 3
    at_end = query(
        values,
        as_of_ns=values[-1].available_ns + 1,
        exclude_event_id=None,
        exclude_contaminated=True,
    )
    assert historical_distribution(values, at_end).count == 3
    missing = replace(
        values[0],
        curve=tuple(
            replace(p, value=None, observed_ns=None) if p.offset_ns == 2 * MINUTE else p
            for p in values[0].curve
        ),
    )
    d = historical_distribution((missing, *values[1:]), query(values))
    assert d.count == 2 and "MISSING_RESPONSE_SAMPLES" in d.reasons
    assert historical_distribution(values, query(values, offset_ns=999)).count == 0
    incomplete = replace(
        values[0],
        quality=values[0].quality | {StudyQuality.EVENT_HISTORY_INCOMPLETE},
    )
    assert (
        "EVENT_HISTORY_INCOMPLETE"
        in historical_distribution((incomplete, *values[1:]), query(values)).reasons
    )


def rebound_surprise(event_id):
    service, ids, _ = historical_service()
    observation = service.engine.surprise(ids[4], EPS_DILUTED, 500, provider_id="SYNTH_A")
    assert observation.normalized_surprise is not None

    def walk(v):
        if isinstance(v, MeasurementContext) and v.event_id == ids[4]:
            return replace(v, event_id=event_id)
        if is_dataclass(v):
            return replace(v, **{f.name: walk(getattr(v, f.name)) for f in fields(v) if f.init})
        if isinstance(v, tuple):
            return tuple(walk(x) for x in v)
        return v

    return walk(observation)


def test_existing_normalized_surprise_conditioning_is_explicit(studies):
    reader, values = studies
    supplied = rebound_surprise(values[0].definition.event.event_id)
    r = result(reader, values[0].definition.event.event_id, 3, surprise=supplied)
    assert r.definition.surprise.result_fingerprint == supplied.result_fingerprint
    conditioned = tuple(
        replace(
            v,
            definition=replace(
                v.definition, surprise=rebound_surprise(v.definition.event.event_id)
            ),
        )
        for v in values
    )
    q = query(
        conditioned,
        surprise_boundaries=(-1.0, 0.0, 1.0),
        surprise_bucket=3,
        measurement=EPS_DILUTED.code,
    )
    assert historical_distribution(conditioned, q).count == 3
    assert historical_distribution(conditioned, replace(q, surprise_bucket=0)).count == 0
    assert historical_distribution(values, q).count == 0
    assert (
        historical_distribution(conditioned, replace(q, measurement="EARNINGS.REVENUE")).count == 0
    )
    no_norm = replace(
        conditioned[0],
        definition=replace(
            conditioned[0].definition,
            surprise=replace(conditioned[0].definition.surprise, normalized_surprise=None),
        ),
    )
    assert historical_distribution((no_norm, *conditioned[1:]), q).count == 2
    with pytest.raises(ValidationError):
        result(
            reader,
            values[0].definition.event.event_id,
            3,
            surprise=replace(supplied, as_of_ns=values[0].anchor_ns + 1),
        )
    with pytest.raises(ValidationError):
        replace(q, measurement=None)


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum_samples": 0},
        {"cohort_fingerprint": "bad"},
        {"context": (("b", "x"), ("a", "y"))},
        {"context": (("a", "x"), ("a", "y"))},
        {"surprise_boundaries": (1.0, 0.0)},
        {"surprise_bucket": 4, "measurement": "EPS"},
    ],
)
def test_distribution_query_validation(studies, changes):
    _, values = studies
    with pytest.raises(ValidationError):
        query(values, **changes)


def test_quantile_interpolation_and_numerical_safeguards():
    assert quantile((0.0, 10.0), 0.25) == 2.5
    assert quantile((10.0,), 0.9) == 10
    assert quantile((0.0, 10.0), 0) == 0 and quantile((0.0, 10.0), 1) == 10
    assert median((1e308, 1e308)) == 1e308
    for samples, p in [((), 0.5), ((1.0,), -1), ((1.0,), float("nan"))]:
        with pytest.raises(ValidationError):
            quantile(samples, p)
    with pytest.raises(ValidationError):
        median(())


def test_individual_and_class_nonparametric_decay_prior_only(studies):
    _, values = studies
    single = decay(values[-1])
    assert single.scope == "SINGLE_EVENT" and single.half_response_time_ns == 2 * MINUTE
    typical = class_decay(values, query(values))
    assert (
        typical.scope == "EVENT_CLASS"
        and typical.half_response_time_ns == single.half_response_time_ns
    )
    assert typical == class_decay(tuple(reversed(values)), query(values))
    assert typical == class_decay(values[:3], query(values))
    assert typical.result_fingerprint
    assert (
        "INSUFFICIENT_OBSERVATIONS"
        in class_decay(values, query(values, minimum_samples=10)).reasons
    )
    other = decay(values[-1], fraction=0.25)
    assert other.half_response_time_ns is None
    with pytest.raises(ValidationError):
        decay(values[-1], fraction=1)


@pytest.mark.parametrize(
    "magnitudes,reason,elapsed",
    [
        ((0, 0, 0), "ZERO_RESPONSE", None),
        ((1, 2, 3), "THRESHOLD_NOT_OBSERVED", None),
        ((None, 1, None), "INSUFFICIENT_OBSERVATIONS", None),
        ((2, 2, 1), None, 2),
        ((None, 4, 1, 1), "INCOMPLETE_CURVE", 1),
    ],
)
def test_decay_peak_ties_missing_threshold(magnitudes, reason, elapsed):
    curve = tuple(InfluencePoint(i, v, int(v is not None)) for i, v in enumerate(magnitudes))
    r = summarize(curve, ("a" * 64,))
    assert r.time_from_peak_ns == elapsed
    if reason:
        assert reason in r.reasons


def test_deterministic_filters(studies):
    _, values = studies
    from eie.backfill.models import ReconstructionQuality
    from eie.events.taxonomy import EventFamily

    assert EventStudyFilter(
        event_types=(EARNINGS.code,),
        families=frozenset({EventFamily.EARNINGS}),
        issuer_id="issuer_fixture",
        reconstruction_quality=ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
    ).matches(values[0])
    for f in (
        EventStudyFilter(start_ns=values[0].anchor_ns + 1),
        EventStudyFilter(end_ns=values[0].anchor_ns),
        EventStudyFilter(require_surprise=True),
        EventStudyFilter(issuer_id="other"),
        EventStudyFilter(families=frozenset({EventFamily.MACRO})),
        EventStudyFilter(event_types=("OTHER.X",)),
        EventStudyFilter(reconstruction_quality=ReconstructionQuality.UNKNOWN),
        EventStudyFilter(exclude_contaminated=True),
    ):
        assert not f.matches(values[-1] if f.exclude_contaminated else values[0])
    with pytest.raises(ValidationError):
        EventStudyFilter(start_ns=2, end_ns=1)


def test_aggregate_validation_checks_prior_evidence_and_decay_quality(studies):
    from eie.studies.aggregation import validate_distribution

    _, values = studies
    q = query(values)
    distribution = historical_distribution(values, q)
    assert validate_distribution(distribution, values)
    assert not validate_distribution(replace(distribution, mean=99), values)
    assert decay(values[-1]).study_quality == values[-1].quality
    assert class_decay(values, q).study_quality == frozenset(
        flag for r in values[:3] for flag in r.quality
    )
