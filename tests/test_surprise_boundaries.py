"""Missingness, compatibility, and finite arithmetic at surprise query boundaries."""

from dataclasses import replace

import pytest
from expectations_helpers import COUNT, ISSUER, USD_EPS, ingest, record, seeded

from eie.core.errors import EIEError, ValidationError
from eie.core.quality import QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.events.revisions import EventRevision, RevisionKind
from eie.expectations.consensus import AggregationMethod, ConsensusOrigin, consensus_snapshot
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import (
    CPI_CORE_MOM,
    EPS_DILUTED,
    GUIDANCE_EPS,
    REVENUE,
    SeasonalAdjustment,
)
from eie.expectations.normalization import HistoricalSurprise, historical_scale
from eie.expectations.observations import ActualObservation, ActualRole
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.surprise import calculate_surprise, with_normalization
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.providers.estimates.adapter import EstimateFailure
from eie.sources.models import SourceHealth, SourceHealthState
from eie.sources.trust import SourceClass


@pytest.fixture
def pair():
    service, (eid,) = seeded()
    ingest(service, [record(eid, 2.4)], 100)
    estimate = service.engine.estimates(eid, EPS_DILUTED, 100).observations[0]
    source = replace(
        estimate.source,
        available_from_ns=200,
        ingested_at_ns=200,
        provider_observed_at_ns=200,
        vintage_at_ns=None,
    )
    actual = ActualObservation(
        estimate.context,
        source,
        Quantity(2.5, estimate.value.unit),
        ActualRole.ACTUAL,
        200,
        EventRevision(1, RevisionKind.ORIGINAL, 200),
    )
    snapshot = service.engine.consensus(eid, EPS_DILUTED, 199)
    return service, estimate, actual, snapshot


@pytest.mark.parametrize(
    "changes",
    [
        {"context": "invalid"},
        {"origin": "PROVIDER"},
        {"method": "MEAN"},
        {"availability": "AVAILABLE"},
        {"value": float("inf")},
        {"age_ns": -1},
        {"as_of_ns": 99},
        {"unit": None},
        {"inputs": ()},
        {"reason_codes": ("Z", "A")},
    ],
)
def test_snapshot_validation(pair, changes):
    with pytest.raises(EIEError):
        replace(pair[3], **changes)


def test_pure_selection_requires_typed_policy(pair):
    service, estimate, _, _ = pair
    with pytest.raises(ValidationError):
        consensus_snapshot(
            estimate.context,
            (estimate,),
            100,
            service.engine.settings.expectations,
            service.engine.settings.source_precedence,
            "config",
            method="MEAN",
        )


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("context", "MEASUREMENT_OR_PERIOD_MISMATCH"),
        ("missing", "CONSENSUS_MISSING"),
        ("vintage", "CONSENSUS_VINTAGE_REQUIRED"),
        ("unit", "INCOMPATIBLE_UNITS"),
        ("overflow", "NUMERIC_OVERFLOW"),
    ],
)
def test_missing_or_incompatible_surprise_never_fabricates_value(pair, mode, reason):
    _, _, actual, snapshot = pair
    if mode == "context":
        actual = replace(actual, context=replace(actual.context, measurement_id=REVENUE))
    elif mode == "missing":
        snapshot = replace(snapshot, value=None, unit=None)
    elif mode == "vintage":
        snapshot = replace(snapshot, point_in_time=False)
    elif mode == "unit":
        actual = replace(actual, value=Quantity(2.5, Unit(UnitKind.COUNT)))
    else:
        actual = replace(actual, value=Quantity(1e308, actual.value.unit))
        snapshot = replace(snapshot, value=-1e308)
    result = calculate_surprise(
        snapshot.context, 200, actual, snapshot, ExpectationsSettings(), "config"
    )
    assert result.raw_surprise is None and reason in result.reason_codes


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("ineligible", "RELATIVE_SURPRISE_INELIGIBLE"),
        ("nearzero", "RELATIVE_CONSENSUS_NEAR_ZERO"),
        ("overflow", "RELATIVE_NUMERIC_OVERFLOW"),
    ],
)
def test_relative_surprise_opt_in_guards(pair, mode, reason):
    _, _, actual, snapshot = pair
    if mode == "ineligible":
        context = replace(actual.context, measurement_id=CPI_CORE_MOM)
        actual = replace(actual, context=context)
        snapshot = replace(snapshot, context=context)
    elif mode == "nearzero":
        snapshot = replace(snapshot, value=0)
    else:
        snapshot = replace(snapshot, value=0.01)
        actual = replace(actual, value=Quantity(1e308, actual.value.unit))
    result = calculate_surprise(
        snapshot.context,
        200,
        actual,
        snapshot,
        ExpectationsSettings(allow_relative_surprise=True),
        "config",
    )
    assert result.relative_surprise is None and reason in result.reason_codes


def test_future_evidence_and_unsorted_reasons_rejected(pair):
    _, _, actual, snapshot = pair
    result = calculate_surprise(actual.context, 200, actual, snapshot, ExpectationsSettings(), "c")
    with pytest.raises(EIEError, match="future"):
        replace(result, as_of_ns=199)
    with pytest.raises(ValidationError, match="sorted"):
        replace(result, reason_codes=("Z", "A"))
    with pytest.raises(ValidationError, match="finite"):
        replace(result, raw_surprise=float("nan"))


def test_actual_provider_conflicts_and_previous_without_consensus():
    service, (eid,) = seeded()
    ingest(
        service,
        [record(eid, 2, kind="ACTUAL", published_at_ns=200, vintage=None, record_id="actual")],
        200,
    )
    ingest(
        service,
        [record(eid, 3, kind="ACTUAL", published_at_ns=200, vintage=None, record_id="actual")],
        200,
        provider="SYNTH_B",
        source_class=SourceClass.OFFICIAL_PRIMARY,
    )

    assert (
        service.engine.surprise(eid, EPS_DILUTED, 200).actual.source.source_class
        is SourceClass.OFFICIAL_PRIMARY
    )
    result = service.engine.surprise(eid, EPS_DILUTED, 200)
    assert result.raw_surprise is None
    assert "CONSENSUS_MISSING" in result.reason_codes
    assert "CONFLICTING_ACTUAL_PROVIDERS" in result.reason_codes
    ingest(
        service,
        [
            record(
                eid,
                1.9,
                kind="ACTUAL",
                role="PREVIOUS",
                vintage=None,
                previous_reference_period={
                    "type": "EARNINGS",
                    "fiscal_year": 2026,
                    "fiscal_quarter": 2,
                    "period_end": "2026-06-30",
                },
                published_at_ns=200,
                record_id="previous",
            )
        ],
        210,
    )
    result = service.engine.surprise(eid, EPS_DILUTED, 210)
    assert len(result.previous) == 1 and result.raw_surprise is None


def test_engine_consensus_missing_provider_single_contributor_and_unit_disagreement():
    service, (eid,) = seeded()
    missing = service.engine.consensus(eid, EPS_DILUTED, 100, origin=ConsensusOrigin.ENGINE)
    assert "ENGINE_CONSENSUS_REQUIRES_PROVIDER" in missing.reason_codes
    ingest(service, [record(eid, 2, estimate_type="INDIVIDUAL_ANALYST", contributor_id="a")], 100)
    one = service.engine.consensus(
        eid, EPS_DILUTED, 100, provider_id="SYNTH_A", origin=ConsensusOrigin.ENGINE
    )
    assert one.value == 2 and one.dispersion is None and one.spread == 0
    ingest(
        service,
        [
            record(
                eid,
                3,
                estimate_type="INDIVIDUAL_ANALYST",
                contributor_id="b",
                record_id="b",
                unit=COUNT,
            )
        ],
        200,
    )
    mixed = service.engine.consensus(
        eid, EPS_DILUTED, 200, provider_id="SYNTH_A", origin=ConsensusOrigin.ENGINE
    )
    assert mixed.value is None and "INCOMPATIBLE_UNITS" in mixed.reason_codes


def test_overflow_in_analyst_statistics_fails_closed():
    service, (eid,) = seeded()
    records = [
        record(
            eid, value, estimate_type="INDIVIDUAL_ANALYST", contributor_id=str(i), record_id=str(i)
        )
        for i, value in enumerate([-1e308, -1e308, 1e308, 1e308])
    ]
    ingest(service, records, 100)
    result = service.engine.consensus(
        eid, EPS_DILUTED, 100, provider_id="SYNTH_A", origin=ConsensusOrigin.ENGINE
    )
    assert result.value is None and "NUMERIC_OVERFLOW" in result.reason_codes


def test_provider_statistics_age_conflict_and_runtime_diagnostics(pair):
    service, estimate, _, snapshot = pair
    assert snapshot.spread is None
    ingest(
        service,
        [
            record(
                estimate.context.event_id,
                8,
                vintage=200,
                record_id="count",
                estimate_type="PROVIDER_CONSENSUS_COUNT",
                unit=COUNT,
            )
        ],
        200,
    )
    result = service.engine.consensus(estimate.context.event_id, EPS_DILUTED, 200)
    assert result.age_ns == 100
    before = canonical_json_bytes(result)
    service.engine.record_health(
        "SYNTH_A",
        200,
        SourceHealth(SourceHealthState.HEALTHY, 200, 200, latency_ns=99, parse_failures=10),
    )
    assert (
        canonical_json_bytes(service.engine.consensus(estimate.context.event_id, EPS_DILUTED, 200))
        == before
    )
    ingest(
        service,
        [
            record(
                estimate.context.event_id,
                3,
                vintage=250,
                record_id="bad-high",
                estimate_type="PROVIDER_CONSENSUS_HIGH",
                unit=COUNT,
            )
        ],
        250,
    )
    incompatible = service.engine.consensus(estimate.context.event_id, EPS_DILUTED, 250)
    assert "INCOMPATIBLE_PROVIDER_STATISTIC" in incompatible.reason_codes
    assert incompatible.high is None
    service.report_failure(
        "SYNTH_A",
        300,
        EstimateFailure.UNAVAILABLE,
    )
    assert (
        QualityFlag.SOURCE_UNAVAILABLE
        in service.engine.consensus(estimate.context.event_id, EPS_DILUTED, 300).quality_flags
    )
    assert replace(snapshot, high=1e308, low=-1e308).spread is None


def history_row(context, at, value, i):
    return HistoricalSurprise(
        replace(context, event_id="evt_" + f"{i:064x}"),
        "EARNINGS.SCHEDULE",
        ISSUER,
        Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD")).canonical_unit,
        at,
        at,
        "SYNTH_A",
        AggregationMethod.MEAN,
        value,
        f"fingerprint-{i}",
    )


def test_normalization_artifact_and_overflow_validation(pair):
    _, _, actual, snapshot = pair
    rows = tuple(
        history_row(actual.context, i, v, i)
        for i, v in enumerate([-1.7e308, -1.7e308, 0, 1.7e308, 1.7e308], 1)
    )
    scale = historical_scale(
        actual.context,
        rows,
        200,
        actual.value.canonical_unit,
        "EARNINGS.SCHEDULE",
        ISSUER,
        "SYNTH_A",
        AggregationMethod.MEAN,
        ExpectationsSettings(),
        "c",
    )
    assert scale.scale is None and "NON_FINITE_SCALE" in scale.reason_codes
    good = replace(scale, center=0, scale=1)
    assert good.result_fingerprint
    for changes in (
        {"scale": 0},
        {"scale": -1},
        {"history": (rows[1], rows[0])},
        {"as_of_ns": 5},
        {
            "history": (
                history_row(actual.context, 1, 2, 9),
                replace(rows[0], context=actual.context),
            )
        },
    ):
        with pytest.raises(ValidationError):
            replace(good, **changes)
    result = calculate_surprise(actual.context, 200, actual, snapshot, ExpectationsSettings(), "c")
    result = replace(result, raw_surprise=1e308)
    overflow = with_normalization(result, replace(good, scale=0.001))
    assert overflow.normalized_surprise is None
    assert "NORMALIZED_NUMERIC_OVERFLOW" in overflow.reason_codes


def test_historical_scale_rejects_incompatible_series_and_future_knowledge(pair):
    _, _, actual, _ = pair
    valid = tuple(history_row(actual.context, i, value, i) for i, value in enumerate((-1, 1, 2), 1))
    assert valid[0].result_fingerprint
    incompatible = []
    changes = (
        {"context": replace(valid[0].context, measurement_id=REVENUE)},
        {"context": replace(valid[0].context, release_stage="FINAL")},
        {"context": replace(valid[0].context, seasonal_adjustment=SeasonalAdjustment.ADJUSTED)},
        {"context": actual.context},
        {"event_type_code": "EARNINGS.RELEASE"},
        {"issuer_id": "iss_" + "f" * 64},
        {"provider_id": "SYNTH_B"},
        {"consensus_method": AggregationMethod.MEDIAN},
        {"unit": Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="EUR")).canonical_unit},
        {"release_at_ns": 200},
        {"available_from_ns": 200},
    )
    for i, change in enumerate(changes, 20):
        row = history_row(actual.context, 10, 999, i)
        incompatible.append(replace(row, **change))
    result = historical_scale(
        actual.context,
        (*valid, *incompatible),
        200,
        actual.value.canonical_unit,
        "EARNINGS.SCHEDULE",
        ISSUER,
        "SYNTH_A",
        AggregationMethod.MEAN,
        ExpectationsSettings(minimum_history=3),
        "c",
    )
    assert result.history == valid
    assert result.scale == pytest.approx(1.4826)


def test_runnable_fixture_example_and_repeatability():
    import runpy
    from pathlib import Path

    build = runpy.run_path(str(Path(__file__).parents[1] / "examples/expectations_replay.py"))[
        "build_example"
    ]
    service, event_id = build()
    second, other_id = build()
    assert event_id == other_id
    assert service.engine.canonical_state_bytes() == second.engine.canonical_state_bytes()
    one = service.engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
    two = second.engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
    assert canonical_json_bytes(one) == canonical_json_bytes(two)
    assert one.normalization.observation_count == 5
    assert one.normalized_surprise == pytest.approx(0.05 / 2.9652)


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "CONSENSUS_MISSING_OR_NON_POINT_IN_TIME"),
        ("unit", "INCOMPATIBLE_UNITS"),
        ("midpoint", "GUIDANCE_MIDPOINT_MISSING"),
        ("overflow", "NUMERIC_OVERFLOW"),
    ],
)
def test_guidance_market_comparison_failure_reasons(mode, reason):
    service, (eid,) = seeded()
    if mode != "missing":
        ingest(
            service,
            [
                record(
                    eid,
                    -1e308 if mode == "overflow" else 2,
                    measurement=GUIDANCE_EPS,
                    unit=COUNT if mode == "unit" else USD_EPS,
                )
            ],
            100,
        )
    source_row = record(
        eid,
        0,
        kind="GUIDANCE",
        measurement=GUIDANCE_EPS,
        vintage=None,
        published_at_ns=200,
        record_id="company",
        issuer_id=ISSUER,
    )
    source_row["low" if mode == "midpoint" else "point"] = {
        "value": 1e308 if mode == "overflow" else 3,
        "unit": USD_EPS,
    }
    ingest(service, [source_row], 200)
    new = service.engine.guidance(eid, GUIDANCE_EPS, 200).observations[0]
    assert isinstance(new, GuidanceObservation)
    result = service.engine.guidance_vs_consensus(new)
    assert result.midpoint_change is None and reason in result.reason_codes
