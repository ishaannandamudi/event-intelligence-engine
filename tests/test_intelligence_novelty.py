"""Prior-only compatible baselines, information changes, and independent confirmations."""

from dataclasses import replace
from itertools import permutations

import pytest

from eie.corporate.types import EarningsPeriod
from eie.evidence.claims import MeasurementSemantics, NumericRange
from eie.evidence.schema import ClaimStatus
from eie.expectations.observations import ActualRole
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.intelligence.engine import calculate
from eie.intelligence.numeric import metric
from eie.intelligence.policy import IntelligencePolicy, NoveltyPolicy
from eie.intelligence.schema import NoveltyState
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.sources.trust import SourceClass
from examples.intelligence_fixtures import USD, batch, source
from tests.intelligence_helpers import compute, ingest, inputs, metrics, novelty

PERIODS = tuple(EarningsPeriod(2025, q, f"2025-{q * 3:02d}-28") for q in range(1, 5))


@pytest.mark.parametrize("repeat_value,count", [(100, 1), (101, 0)])
def test_repeated_reporting_period_is_one_sample_and_conflicting_period_is_excluded(
    repeat_value, count
):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        for i, value in enumerate((100, repeat_value)):
            ingest(
                store,
                p,
                batch(p, 10 + i, Quantity(value, USD), period=PERIODS[0], key=f"prior-{i}"),
            )
        new = batch(p, 30, Quantity(120, USD))
        ingest(store, p, new)
        c = novelty(compute(store, new), "NUMERIC")[0]
        assert metrics(c)["prior_sample_count"] == count
        if count == 0:
            assert "AMBIGUOUS_PERIOD_BASELINE_EXCLUDED" in c.reasons


def test_numeric_overflow_is_explicit_in_displacements_and_robust_scale():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        for i, v in enumerate((-1e308, -1e308, 1e308, 1e308)):
            ingest(store, p, batch(p, 10 + i, Quantity(v, USD), period=PERIODS[i]))
        new = batch(p, 30, Quantity(1e308, USD))
        ingest(store, p, new)
        c = novelty(compute(store, new), "NUMERIC")[0]
        assert all(m.value is None or m.value == m.value for m in c.metrics)
        assert any(m.reason == "NUMERIC_OVERFLOW" for m in c.metrics)


def test_numeric_strict_prior_current_exclusion_mad_and_restart(tmp_path):
    path = tmp_path / "intel.sqlite"
    with SQLiteEventStore(path) as store:
        p = source()
        for i, value in enumerate((100, 110, 120)):
            ingest(store, p, batch(p, 10 + i, Quantity(value, USD), period=PERIODS[i]))
        current = batch(p, 30, Quantity(150, USD))
        ingest(store, p, current)
        result = compute(store, current)
        numeric = novelty(result, "NUMERIC")[0]
        assert metrics(numeric) == {
            "absolute_change": 30,
            "high_displacement": 30,
            "low_displacement": 30,
            "median": 110,
            "median_absolute_deviation": 10,
            "prior_sample_count": 3,
            "relative_change": 0.25,
            "standardized_mad": 4,
        }
        assert current.events[0].event_id in result.manifest.excluded_event_ids
        assert len(numeric.prior_ids) == 3
        same = batch(p, 30, Quantity(999, USD), key="simultaneous", period=PERIODS[3])
        # Unsealed same-time fixtures must arrive before calculating either result.
        saved = dumps(result)
        future = batch(p, 40, Quantity(999, USD), key="future", period=PERIODS[3])
        ingest(store, p, future)
        assert dumps(compute(store, current)) == saved
        assert same.raw.observation_id not in {r.identity for r in result.manifest.prior}
        identity = result.intelligence_id
    from eie.intelligence.repository import IntelligenceRepository
    from eie.intelligence.validation import validate

    with SQLiteEventStore(path) as store:
        assert dumps(IntelligenceRepository(store).get(identity)) == saved
        assert validate(IntelligenceRepository(store), identity).state.value != "INVALID"


@pytest.mark.parametrize(
    "prior_values,current,reason",
    [
        ((0, 0, 0), 1, "NEAR_ZERO_BASELINE"),
        ((100, 100, 100), 120, "ZERO_MAD"),
        ((1e-12, 1e-12, 1e-12), 1, "NEAR_ZERO_BASELINE"),
        ((10,), 20, "INSUFFICIENT_SAMPLES"),
    ],
)
def test_zero_and_small_baselines_are_unavailable(prior_values, current, reason):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        for i, v in enumerate(prior_values):
            ingest(store, p, batch(p, 10 + i, Quantity(v, USD), period=PERIODS[i]))
        new = batch(p, 30, Quantity(current, USD))
        ingest(store, p, new)
        c = novelty(compute(store, new), "NUMERIC")[0]
        assert reason in c.reasons
        assert metrics(c)["standardized_mad"] is None


@pytest.mark.parametrize(
    "difference",
    ["currency", "measurement", "entity", "frequency", "role", "stage", "seasonal", "type"],
)
def test_numeric_context_mismatch_never_enters_cohort(difference):
    from eie.events.taxonomy import EventFamily, EventType
    from eie.expectations.models import SeasonalAdjustment

    with SQLiteEventStore(":memory:") as store:
        p = source()
        options = {"period": PERIODS[0]}
        value = Quantity(10, USD)
        if difference == "currency":
            value = Quantity(10, Unit(UnitKind.CURRENCY, currency="EUR"))
        if difference == "measurement":
            options["measurement"] = "EARNINGS.ASSETS"
        if difference == "entity":
            options["subject_id"] = "iss_" + "b" * 64
        if difference == "frequency":
            options["period"] = EarningsPeriod(2025, None, "2025-03-28")
        if difference == "role":
            options["semantics"] = MeasurementSemantics(
                actual_role=ActualRole.PREVIOUS,
                previous_period=EarningsPeriod(2024, 1, "2024-03-28"),
            )
        if difference == "type":
            options["event_type"] = EventType(EventFamily.EARNINGS, "EARNINGS.SCHEDULE")
        prior = batch(p, 10, value, **options)
        if difference in ("stage", "seasonal"):
            c = prior.claims[0].claim
            context = (
                replace(c.measurement, release_stage="ADVANCE")
                if difference == "stage"
                else replace(c.measurement, seasonal_adjustment=SeasonalAdjustment.ADJUSTED)
            )
            prior = replace(
                prior, claims=(replace(prior.claims[0], claim=replace(c, measurement=context)),)
            )
        ingest(store, p, prior)
        new = batch(p, 20, Quantity(20, USD))
        ingest(store, p, new)
        c = novelty(compute(store, new), "NUMERIC")[0]
        assert c.state is NoveltyState.NO_BASELINE and c.prior_ids == ()


def test_range_baseline_retains_bounds_without_invented_point():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        first = batch(p, 10, NumericRange(Quantity(10, USD), Quantity(20, USD)), period=PERIODS[0])
        new = batch(p, 20, NumericRange(Quantity(25, USD), Quantity(40, USD)))
        ingest(store, p, first)
        ingest(store, p, new)
        c = novelty(compute(store, new), "NUMERIC")[0]
        assert metrics(c)["low_displacement"] == 15 and metrics(c)["high_displacement"] == 20
        assert metrics(c)["absolute_change"] is None and metrics(c)["median"] is None


@pytest.mark.parametrize(
    "status,expected",
    [
        (ClaimStatus.CORRECTED, "CORRECTION"),
        (ClaimStatus.UPDATED, "UPDATED_FACT"),
        (ClaimStatus.RETRACTED, "RETRACTION"),
        (ClaimStatus.WITHDRAWN, "RETRACTION"),
    ],
)
def test_same_proposition_correction_and_retraction_remain_meaningful(status, expected):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        old = batch(p, 10, Quantity(100, USD))
        ingest(store, p, old)
        before = compute(store, old)
        new = batch(p, 20, Quantity(110, USD), prior=old, status=status)
        ingest(store, p, new)
        result = compute(store, new)
        assert novelty(result, "FACTUAL")[0].state.value == expected
        assert novelty(result, "FACTUAL")[0].prior_ids
        assert dumps(compute(store, old)) == dumps(before)
        assert novelty(result, "NUMERIC")[0].prior_ids == ()  # independent pool excludes same event


@pytest.mark.parametrize(
    "family,changed,expected",
    [
        ("origin", False, "SYNDICATED_REPEAT"),
        ("independent", False, "INDEPENDENT_CONFIRMATION"),
        ("independent", True, "CONTRADICTION"),
    ],
)
def test_source_families_distinct_from_factual_newness(family, changed, expected):
    with SQLiteEventStore(":memory:") as store:
        a = source("ORIGIN", family="origin")
        b = source("MIRROR", family=family)
        first = batch(a, 10, Quantity(100, USD), key="original")
        ingest(store, a, first)
        new = batch(b, 20, Quantity(101 if changed else 100, USD), key="repeat")
        ingest(store, b, new)
        result = compute(store, new)
        assert novelty(result, "CROSS_SOURCE")[0].state.value == expected
        assert novelty(result, "FACTUAL")[0].state.value == (
            "UPDATED_FACT" if changed else "KNOWN_ALREADY"
        )


@pytest.mark.parametrize(
    "kind,expected",
    [
        (SourceClass.COMPANY_PRIMARY, "NEW_PRIMARY_INFORMATION"),
        (SourceClass.SECONDARY_NEWS, "NEW_SECONDARY_INFORMATION"),
    ],
)
def test_first_information_trust_does_not_mean_confidence_probability(kind, expected):
    with SQLiteEventStore(":memory:") as store:
        p = source(source_class=kind)
        new = batch(p, 20)
        ingest(store, p, new)
        assert novelty(compute(store, new), "CROSS_SOURCE")[0].state.value == expected


def test_guidance_changes_are_company_history_not_consensus():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        old = batch(
            p,
            10,
            NumericRange(Quantity(100, USD), Quantity(120, USD)),
            key="old-guidance",
            measurement="GUIDANCE.REVENUE",
        )
        ingest(store, p, old)
        new = batch(
            p,
            20,
            NumericRange(Quantity(130, USD), Quantity(160, USD)),
            key="new-guidance",
            measurement="GUIDANCE.REVENUE",
        )
        ingest(store, p, new)
        c = novelty(compute(store, new), "GUIDANCE")[0]
        assert c.state.value == "CHANGED"
        assert metrics(c) == {
            "high_change": 40,
            "low_change": 30,
            "midpoint_change": 35,
            "relative_midpoint_change": 35 / 110,
            "width_change": 10,
        }


@pytest.mark.parametrize("withdraw", [False, True])
def test_guidance_initiation_withdrawal_has_no_fabricated_displacement(withdraw):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        first = batch(p, 10, Quantity(100, USD), measurement="GUIDANCE.REVENUE")
        ingest(store, p, first)
        target = first
        if withdraw:
            target = batch(
                p,
                20,
                Quantity(100, USD),
                measurement="GUIDANCE.REVENUE",
                prior=first,
                status=ClaimStatus.WITHDRAWN,
            )
            ingest(store, p, target)
        c = novelty(compute(store, target), "GUIDANCE")[0]
        assert c.state.value == ("WITHDRAWN" if withdraw else "INITIATED")
        assert all(m.value is None for m in c.metrics)


def test_latest_sample_window_is_deterministic_and_policy_changes_identity():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        for i, period in enumerate(PERIODS):
            ingest(store, p, batch(p, 10 + i, Quantity(10 + i, USD), period=period))
        new = batch(p, 20)
        ingest(store, p, new)
        a = compute(store, new)
        policy = IntelligencePolicy(novelty=NoveltyPolicy(historical_samples=3))
        b = compute(store, new, policy=policy)
        assert a.intelligence_id != b.intelligence_id
        assert metrics(novelty(b, "NUMERIC")[0])["prior_sample_count"] == 3
        assert "HISTORICAL_SAMPLE_WINDOW" in novelty(b, "NUMERIC")[0].reasons
        data = inputs(store, new, policy=policy)
        for order in permutations(data.prior):
            assert calculate(replace(data, prior=order), policy) == calculate(data, policy)


def test_metric_overflow_is_explicit():
    assert metric("overflow", float("inf")).reason == "NUMERIC_OVERFLOW"
