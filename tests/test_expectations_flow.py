"""End-to-end vintage, consensus, surprise, guidance, and prior-history invariants."""

from pathlib import Path

import pytest
from expectations_helpers import (
    COUNT,
    ISSUER,
    MACRO_PERIOD,
    PERCENT,
    USD,
    USD_EPS,
    adapter,
    historical_service,
    ingest,
    payload,
    record,
    seeded,
)

from eie.config.settings import Settings
from eie.core.errors import TemporalConsistencyError
from eie.core.quality import QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.corporate.types import EarningsPeriod
from eie.expectations.consensus import AggregationMethod, ConsensusOrigin
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import (
    CPI_HEADLINE_MOM,
    EPS_DILUTED,
    GUIDANCE_EPS,
    NFP_PAYROLL_CHANGE,
    REVENUE,
)
from eie.expectations.queries import EstimateAvailability
from eie.expectations.settings import (
    CutoffPolicy,
    ExpectationsSettings,
    ProviderPooling,
    ScaleMethod,
)
from eie.providers.estimates.adapter import EstimateFailure
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass

FIXTURES = Path(__file__).parent / "fixtures" / "estimates"


def test_fixture_vintages_conflicts_and_frozen_release_consensus() -> None:
    service, (event_id,) = seeded()
    source = adapter()
    for name, at in (("provider_a_t1", 100), ("provider_a_t2", 200)):
        result = service.ingest_payload(source, (FIXTURES / f"{name}.json").read_bytes(), at, at)
        assert result.observations_created == 1
    assert service.engine.consensus(event_id, EPS_DILUTED, 150).value == 2.40
    before = canonical_json_bytes(service.engine.consensus(event_id, EPS_DILUTED, 150))
    assert service.engine.consensus(event_id, EPS_DILUTED, 250).value == 2.45
    b = service.ingest_payload(
        adapter("SYNTH_B"), (FIXTURES / "provider_b.json").read_bytes(), 210, 210
    )
    assert b.observations_created == 1
    conflict = service.engine.consensus(event_id, EPS_DILUTED, 250)
    assert len(conflict.provider_claims) == 2
    assert QualityFlag.CONFLICTING_SOURCES in conflict.quality_flags
    assert {v.contributor_count for v in conflict.provider_claims} == {12, 18}
    assert service.engine.surprise(event_id, EPS_DILUTED, 250).raw_surprise is None
    ingest(
        service,
        [
            record(
                event_id,
                2.50,
                kind="ACTUAL",
                vintage=None,
                record_id="eps-actual",
                published_at_ns=300,
            )
        ],
        300,
    )
    surprise = service.engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
    assert surprise.raw_surprise == pytest.approx(0.05)
    assert surprise.consensus.value == 2.45
    assert surprise.consensus.as_of_ns == 299
    original = canonical_json_bytes(surprise)
    ingest(service, [record(event_id, 2.47, vintage=200, contributor_count=13)], 400)
    assert service.engine.consensus(event_id, EPS_DILUTED, 400, provider_id="SYNTH_A").value == 2.47
    assert (
        canonical_json_bytes(
            service.engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
        )
        == original
    )
    assert (
        service.engine.surprise(event_id, EPS_DILUTED, 400, provider_id="SYNTH_A").raw_surprise
        == surprise.raw_surprise
    )
    assert canonical_json_bytes(service.engine.consensus(event_id, EPS_DILUTED, 150)) == before
    assert len(service.engine.estimates(event_id, EPS_DILUTED, 400).observations) == 4


@pytest.mark.parametrize(
    "policy,expected", [(CutoffPolicy.STRICT_BEFORE, 2.4), (CutoffPolicy.AT_OR_BEFORE, 2.45)]
)
def test_exact_release_cutoff_and_delayed_actual(policy, expected) -> None:
    settings = Settings(
        expectations=ExpectationsSettings(enabled=True, consensus_cutoff_policy=policy)
    )
    service, (event_id,) = seeded(settings=settings)
    ingest(service, [record(event_id, 2.4, vintage=100)], 100)
    ingest(service, [record(event_id, 2.45, vintage=300)], 300)
    ingest(service, [record(event_id, 2.49, vintage=310)], 310)
    ingest(
        service,
        [
            record(
                event_id, 2.5, kind="ACTUAL", record_id="actual", vintage=None, published_at_ns=300
            )
        ],
        320,
    )
    assert service.engine.surprise(event_id, EPS_DILUTED, 319).actual is None
    assert service.engine.surprise(event_id, EPS_DILUTED, 320).consensus.value == expected
    ingest(
        service,
        [
            record(
                event_id, 2.6, kind="ACTUAL", record_id="actual", vintage=None, published_at_ns=400
            )
        ],
        400,
    )
    corrected = service.engine.surprise(event_id, EPS_DILUTED, 400)
    assert corrected.consensus.value == expected
    assert corrected.release_at_ns == 300
    assert corrected.actual.revision.number == 2
    assert service.engine.surprise(event_id, EPS_DILUTED, 320).actual.value.value == 2.5


@pytest.mark.parametrize(
    "method,expected",
    [
        (AggregationMethod.MEAN, 3),
        (AggregationMethod.MEDIAN, 2.5),
        (AggregationMethod.HIGH, 6),
        (AggregationMethod.LOW, 1),
        (AggregationMethod.COUNT, 4),
    ],
)
def test_analyst_aggregation_is_explicit_and_provider_scoped(method, expected) -> None:
    service, (event_id,) = seeded()
    records = [
        record(
            event_id,
            value,
            record_id=f"analyst-{i}",
            contributor_id=f"analyst-{i}",
            estimate_type="INDIVIDUAL_ANALYST",
        )
        for i, value in enumerate((1, 2, 3, 6))
    ]
    records.append(record(event_id, 99, record_id="anonymous", estimate_type="INDIVIDUAL_ANALYST"))
    ingest(service, records, 100)
    ingest(
        service,
        [
            record(
                event_id,
                99,
                record_id="b-analyst",
                contributor_id="analyst-0",
                estimate_type="INDIVIDUAL_ANALYST",
            )
        ],
        100,
        provider="SYNTH_B",
    )
    result = service.engine.consensus(
        event_id,
        EPS_DILUTED,
        100,
        provider_id="SYNTH_A",
        origin=ConsensusOrigin.ENGINE,
        method=method,
    )
    assert result.value == expected
    assert result.contributor_count == 4
    assert result.dispersion == pytest.approx(2.160246899469287)
    assert result.interquartile_range == 3
    assert result.high == 6 and result.low == 1
    assert "CONTRIBUTOR_ID_MISSING" in result.reason_codes
    ingest(
        service,
        [
            record(
                event_id,
                4,
                record_id="analyst-0",
                contributor_id="analyst-0",
                estimate_type="INDIVIDUAL_ANALYST",
                vintage=200,
            )
        ],
        200,
    )
    assert (
        service.engine.consensus(
            event_id, EPS_DILUTED, 200, provider_id="SYNTH_A", origin=ConsensusOrigin.ENGINE
        ).value
        == 3.75
    )
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 100, origin=ConsensusOrigin.ENGINE).value
        is None
    )


def test_provider_statistics_staleness_missing_vintage_and_health() -> None:
    settings = Settings(expectations=ExpectationsSettings(enabled=True, staleness_ns=50))
    service, (event_id,) = seeded(settings=settings)
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 10).availability
        is EstimateAvailability.COVERAGE_UNKNOWN
    )
    coverage = {"event_ids": [event_id], "measurements": [EPS_DILUTED.code], "has_vintages": False}
    ingest(service, [], 20, coverage=coverage)
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 20).availability
        is EstimateAvailability.NO_ESTIMATE_AVAILABLE
    )
    service.report_failure("SYNTH_A", 30, EstimateFailure.UNAVAILABLE)
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 30).availability
        is EstimateAvailability.PROVIDER_UNAVAILABLE
    )
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 20).availability
        is EstimateAvailability.NO_ESTIMATE_AVAILABLE
    )
    ingest(
        service,
        [
            record(event_id, 2.4, vintage=None),
            record(event_id, 3, record_id="high", estimate_type="PROVIDER_CONSENSUS_HIGH"),
            record(event_id, 2, record_id="low", estimate_type="PROVIDER_CONSENSUS_LOW"),
            record(
                event_id,
                12,
                record_id="count",
                unit=COUNT,
                estimate_type="PROVIDER_CONSENSUS_COUNT",
            ),
        ],
        100,
    )
    latest = service.engine.consensus(event_id, EPS_DILUTED, 100)
    assert latest.high == 3 and latest.low == 2 and latest.contributor_count == 12
    assert not latest.point_in_time and latest.dispersion is None
    assert (
        service.engine.consensus(event_id, EPS_DILUTED, 151).availability
        is EstimateAvailability.STALE_ESTIMATE
    )
    ingest(
        service,
        [
            record(
                event_id, 2.5, kind="ACTUAL", record_id="actual", published_at_ns=200, vintage=None
            )
        ],
        200,
    )
    assert (
        "CONSENSUS_VINTAGE_REQUIRED"
        in service.engine.surprise(event_id, EPS_DILUTED, 200).reason_codes
    )
    assert service.engine.health_as_of("SYNTH_A", 251).state is SourceHealthState.STALE


def test_macro_previous_revised_previous_and_distinct_components() -> None:
    service, (event_id,) = seeded(periods=(MACRO_PERIOD,), macro=True)
    prior = {
        "type": "MACRO",
        "start_date": "2026-07-01",
        "end_date": "2026-08-01",
        "frequency": "MONTHLY",
        "label": "2026-07",
    }
    ingest(
        service,
        [
            record(
                event_id,
                140,
                measurement=NFP_PAYROLL_CHANGE,
                period=MACRO_PERIOD,
                unit={"kind": "COUNT", "magnitude": "THOUSAND"},
                record_id="nfp",
            ),
            record(
                event_id,
                0.3,
                measurement=CPI_HEADLINE_MOM,
                period=MACRO_PERIOD,
                unit=PERCENT,
                record_id="cpi",
            ),
        ],
        100,
    )
    actuals = [
        record(
            event_id,
            value,
            measurement=measurement,
            period=MACRO_PERIOD,
            unit=unit,
            record_id=rid,
            kind="ACTUAL",
            published_at_ns=200,
            vintage=None,
            **extras,
        )
        for value, measurement, unit, rid, extras in (
            (160000, NFP_PAYROLL_CHANGE, COUNT, "nfp-actual", {}),
            (
                150000,
                NFP_PAYROLL_CHANGE,
                COUNT,
                "previous",
                {"role": "PREVIOUS", "previous_reference_period": prior},
            ),
            (0.4, CPI_HEADLINE_MOM, PERCENT, "cpi-actual", {}),
        )
    ]
    ingest(service, actuals, 200)
    original = service.engine.surprise(event_id, NFP_PAYROLL_CHANGE, 200)
    before = canonical_json_bytes(original)
    assert original.raw_surprise == 20000
    assert original.previous[0].value.canonical_value == 150000
    assert original.revised_previous == ()
    assert service.engine.surprise(event_id, CPI_HEADLINE_MOM, 200).raw_surprise == pytest.approx(
        0.001
    )
    ingest(
        service,
        [
            record(
                event_id,
                135000,
                measurement=NFP_PAYROLL_CHANGE,
                period=MACRO_PERIOD,
                unit=COUNT,
                kind="ACTUAL",
                record_id="revised",
                role="REVISED_PREVIOUS",
                published_at_ns=300,
                vintage=None,
                previous_reference_period=prior,
            )
        ],
        300,
    )
    result = service.engine.surprise(event_id, NFP_PAYROLL_CHANGE, 300)
    assert result.previous[0].value.canonical_value == 150000
    assert result.revised_previous[0].value.canonical_value == 135000
    assert (
        canonical_json_bytes(service.engine.surprise(event_id, NFP_PAYROLL_CHANGE, 200)) == before
    )


def test_eps_revenue_units_and_relative_surprise_remain_separate() -> None:
    settings = Settings(
        expectations=ExpectationsSettings(enabled=True, allow_relative_surprise=True)
    )
    service, (event_id,) = seeded(settings=settings)
    ingest(
        service,
        [
            record(event_id, -2, record_id="eps"),
            record(
                event_id,
                14.2,
                measurement=REVENUE,
                record_id="revenue",
                unit={"kind": "CURRENCY", "currency": "USD", "magnitude": "BILLION"},
            ),
        ],
        100,
    )
    ingest(
        service,
        [
            record(
                event_id,
                -1.5,
                kind="ACTUAL",
                published_at_ns=200,
                vintage=None,
                record_id="eps-actual",
            ),
            record(
                event_id,
                14500000000,
                measurement=REVENUE,
                unit=USD,
                kind="ACTUAL",
                published_at_ns=200,
                vintage=None,
                record_id="rev-actual",
            ),
        ],
        200,
    )
    eps = service.engine.surprise(event_id, EPS_DILUTED, 200)
    revenue = service.engine.surprise(event_id, REVENUE, 200)
    assert eps.raw_surprise == 0.5 and eps.relative_surprise == 0.25
    assert revenue.raw_surprise == 300000000
    assert revenue.relative_surprise == pytest.approx(300000000 / 14200000000)
    assert eps.context != revenue.context


@pytest.mark.parametrize("method", [ScaleMethod.MAD, ScaleMethod.STANDARD_DEVIATION])
def test_prior_only_scale_same_time_future_events_and_corrections(method) -> None:
    settings = Settings(
        expectations=ExpectationsSettings(
            enabled=True, minimum_history=3, normalization_method=method
        )
    )
    service, ids, periods = historical_service(settings=settings)
    ingest(
        service,
        [
            record(ids[-2], 10, period=periods[-2], record_id="new-c", vintage=550),
            record(ids[-1], 10, period=periods[-1], record_id="same-c", vintage=550),
        ],
        550,
    )
    ingest(
        service,
        [
            record(
                ids[-2],
                12,
                period=periods[-2],
                kind="ACTUAL",
                record_id="new-a",
                published_at_ns=600,
                vintage=None,
            ),
            record(
                ids[-1],
                1000,
                period=periods[-1],
                kind="ACTUAL",
                record_id="same-a",
                published_at_ns=600,
                vintage=None,
            ),
        ],
        600,
    )
    first = service.engine.surprise(ids[-2], EPS_DILUTED, 600)
    second = service.engine.surprise(ids[-1], EPS_DILUTED, 600)
    assert first.normalization.observation_count == 5
    assert first.normalization.scale == second.normalization.scale
    assert {v.context.event_id for v in first.normalization.history} == set(ids[:5])
    assert all(
        v.available_from_ns < 600 and v.release_at_ns < 600 for v in first.normalization.history
    )
    assert first.normalized_surprise == pytest.approx(2 / first.normalization.scale)
    before = canonical_json_bytes(first)
    ingest(
        service,
        [
            record(
                ids[0],
                10000,
                period=periods[0],
                kind="ACTUAL",
                record_id="a-0",
                published_at_ns=700,
                vintage=None,
            )
        ],
        700,
    )
    assert (
        service.engine.surprise(ids[-2], EPS_DILUTED, 700).normalization.scale
        == first.normalization.scale
    )
    assert canonical_json_bytes(service.engine.surprise(ids[-2], EPS_DILUTED, 600)) == before


@pytest.mark.parametrize(
    "values,reason", [((1, 1, 1), "ZERO_SCALE"), ((1, 2), "INSUFFICIENT_HISTORY")]
)
def test_zero_and_insufficient_scale_fail_closed(values, reason) -> None:
    service, ids, periods = historical_service(values)
    at = (len(values) + 1) * 100
    ingest(
        service,
        [record(ids[-1], 10, period=periods[-1], record_id="c-final", vintage=at - 10)],
        at - 10,
    )
    ingest(
        service,
        [
            record(
                ids[-1],
                11,
                period=periods[-1],
                kind="ACTUAL",
                record_id="a-final",
                published_at_ns=at,
                vintage=None,
            )
        ],
        at,
    )
    result = service.engine.surprise(ids[-1], EPS_DILUTED, at)
    assert result.normalized_surprise is None
    assert reason in result.reason_codes


@pytest.mark.parametrize(
    "pool,expected", [(ProviderPooling.SAME_PROVIDER, None), (ProviderPooling.EXPLICIT_POOL, True)]
)
def test_provider_specific_and_explicit_pooled_history(pool, expected) -> None:
    settings = Settings(
        expectations=ExpectationsSettings(
            enabled=True, minimum_history=3, history_window_events=3, provider_pooling_policy=pool
        )
    )
    service, ids, periods = historical_service(settings=settings, provider="SYNTH_B")
    ingest(service, [record(ids[-1], 10, period=periods[-1], record_id="c-last", vintage=550)], 550)
    ingest(
        service,
        [
            record(
                ids[-1],
                12,
                period=periods[-1],
                record_id="a-last",
                kind="ACTUAL",
                published_at_ns=600,
                vintage=None,
            )
        ],
        600,
    )
    result = service.engine.surprise(ids[-1], EPS_DILUTED, 600)
    if expected is None:
        assert result.normalized_surprise is None
    else:
        assert result.normalized_surprise is not None
        assert result.normalization.observation_count == 3
        assert "POOLED_PROVIDER_HISTORY" in result.reason_codes
        assert QualityFlag.DEGRADED in result.quality_flags


def test_guidance_ranges_revision_and_market_comparison_are_distinct() -> None:
    service, (event_id,) = seeded()
    future = EarningsPeriod(2026, 4, "2026-12-31")
    ingest(
        service,
        [
            record(
                event_id,
                3,
                measurement=GUIDANCE_EPS,
                period=future,
                record_id="market-guidance",
                vintage=100,
            )
        ],
        100,
    )

    def guidance(low, high, point=None):
        result = record(
            event_id,
            0,
            measurement=GUIDANCE_EPS,
            period=future,
            record_id="company-guidance",
            kind="GUIDANCE",
            vintage=None,
            issuer_id=ISSUER,
            published_at_ns=200,
        )
        result.pop("quantity")
        for name, value in (("low", low), ("high", high), ("point", point)):
            if value is not None:
                result[name] = {"value": value, "unit": USD_EPS}
        return result

    ingest(service, [guidance(2, 4)], 200, source_class=SourceClass.COMPANY_PRIMARY)
    old = service.engine.guidance(event_id, GUIDANCE_EPS, 200).observations[0]
    assert isinstance(old, GuidanceObservation) and old.midpoint == 3
    original = canonical_json_bytes(service.engine.guidance(event_id, GUIDANCE_EPS, 200))
    new_payload = guidance(3, 5, 4)
    new_payload["published_at_ns"] = 300
    ingest(service, [new_payload], 300, source_class=SourceClass.COMPANY_PRIMARY)
    new = service.engine.guidance(event_id, GUIDANCE_EPS, 300).observations[-1]
    change = service.engine.guidance_change(new)
    market = service.engine.guidance_vs_consensus(new)
    assert change.midpoint_change == change.low_change == change.high_change == 1
    assert market.midpoint_change == 1 and market.prior is None
    assert change.comparison != market.comparison
    assert canonical_json_bytes(service.engine.guidance(event_id, GUIDANCE_EPS, 200)) == original
    assert service.engine.guidance_change(old).midpoint_change is None


def test_atomic_batch_idempotency_schema_drift_and_byte_identical_builds() -> None:
    service, (event_id,) = seeded()
    initial = payload([record(event_id, 2.4)])
    first = service.ingest_payload(adapter(), initial, 100, 100)
    assert first.observations_created == 1
    state = service.engine.canonical_state_bytes()
    duplicate = service.ingest_payload(adapter(), initial, 200, 200)
    assert duplicate.observations_created == 0 and duplicate.duplicates_suppressed == 1
    assert service.engine.canonical_state_bytes() == state
    bad = record("evt_" + "f" * 64, 2, record_id="unknown")
    result = service.ingest_payload(
        adapter(), payload([record(event_id, 2.45, vintage=300), bad], item_id="bad"), 300, 300
    )
    assert result.error_category is EstimateFailure.NORMALIZATION_ERROR
    assert service.engine.canonical_state_bytes() == state
    schema = service.ingest_payload(adapter(), b'{"schema":"bad"}', 400, 400)
    assert schema.health.state is SourceHealthState.SCHEMA_MISMATCH
    parse = service.ingest_payload(adapter(), b"{", 500, 500)
    assert parse.error_category is EstimateFailure.PARSE_ERROR
    assert service.engine.canonical_state_bytes() == state
    with pytest.raises(TemporalConsistencyError):
        service.ingest_payload(adapter(), initial, 600, 500)

    def build():
        obj, (eid,) = seeded()
        ingest(obj, [record(eid, 2.4)], 100)
        ingest(
            obj,
            [
                record(
                    eid, 2.5, kind="ACTUAL", published_at_ns=200, vintage=None, record_id="actual"
                )
            ],
            200,
        )
        return obj.engine.canonical_state_bytes(), canonical_json_bytes(
            obj.engine.surprise(eid, EPS_DILUTED, 200)
        )

    assert build() == build()
