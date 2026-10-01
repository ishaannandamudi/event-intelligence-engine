"""Boundary validation of numeric, temporal, identity, and immutable contracts."""

from dataclasses import replace

import pytest
from expectations_helpers import ISSUER, MACRO_PERIOD, PERIOD, ingest, record, seeded

from eie.config.settings import Settings
from eie.core.errors import EIEError, PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.events.revisions import EventRevision, RevisionKind
from eie.expectations.guidance import GuidanceObservation, compare_guidance
from eie.expectations.history import ObservationLedger
from eie.expectations.models import (
    CPI_CORE_MOM,
    EPS_DILUTED,
    GUIDANCE_EPS,
    EventReference,
    MeasurementId,
    flags,
    text,
)
from eie.expectations.observations import ActualObservation, ActualRole, EstimateKind
from eie.expectations.queries import EstimateCoverage, ObservationHistory
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.units import CanonicalUnit, Dimension, Magnitude, Quantity, Unit, UnitKind
from eie.persistence.memory import InMemoryEventStore


@pytest.fixture
def sample():
    service, (event_id,) = seeded()
    ingest(service, [record(event_id, 2.4)], 100)
    row = service.engine.estimates(event_id, EPS_DILUTED, 100).observations[0]
    return service, row


@pytest.mark.parametrize(
    "kind,magnitude,currency,expected,dimension",
    [
        (UnitKind.CURRENCY, Magnitude.MILLION, "USD", 2_000_000, Dimension.CURRENCY),
        (UnitKind.CURRENCY_PER_SHARE, Magnitude.ONE, "EUR", 2, Dimension.CURRENCY_PER_SHARE),
        (UnitKind.COUNT, Magnitude.THOUSAND, None, 2000, Dimension.COUNT),
        (UnitKind.COUNT, Magnitude.BILLION, None, 2_000_000_000, Dimension.COUNT),
        (UnitKind.PERCENT, Magnitude.ONE, None, 0.02, Dimension.RATE),
        (UnitKind.PERCENTAGE_POINTS, Magnitude.ONE, None, 0.02, Dimension.RATE_CHANGE),
        (UnitKind.BASIS_POINTS, Magnitude.ONE, None, 0.0002, Dimension.RATE_CHANGE),
        (UnitKind.ANNUALIZED_GROWTH, Magnitude.ONE, None, 0.02, Dimension.ANNUALIZED_RATE),
        (UnitKind.INDEX_LEVEL, Magnitude.ONE, None, 2, Dimension.INDEX_LEVEL),
        (UnitKind.RATIO, Magnitude.ONE, None, 2, Dimension.RATIO),
    ],
)
def test_safe_unit_conversions(kind, magnitude, currency, expected, dimension):
    q = Quantity(2, Unit(kind, magnitude, currency))
    assert q.value == 2
    assert q.canonical_value == pytest.approx(expected)
    assert q.canonical_unit == CanonicalUnit(dimension, currency)
    assert canonical_json_bytes(q) == canonical_json_bytes(q)


@pytest.mark.parametrize("value", [True, "2", float("nan"), float("inf"), -float("inf"), 10**400])
def test_nonfinite_and_non_numeric_values_rejected(value):
    with pytest.raises(ValidationError):
        Quantity(value, Unit(UnitKind.COUNT))


@pytest.mark.parametrize(
    "build",
    [
        lambda: Unit("COUNT"),
        lambda: Unit(UnitKind.COUNT, "ONE"),
        lambda: Unit(UnitKind.CURRENCY),
        lambda: Unit(UnitKind.COUNT, currency="USD"),
        lambda: Unit(UnitKind.CURRENCY, currency="usd"),
        lambda: Unit(UnitKind.PERCENT, Magnitude.MILLION),
        lambda: CanonicalUnit("COUNT"),
        lambda: CanonicalUnit(Dimension.CURRENCY),
        lambda: CanonicalUnit(Dimension.COUNT, "USD"),
        lambda: Quantity(1, "COUNT"),
        lambda: Quantity(1e308, Unit(UnitKind.CURRENCY, Magnitude.BILLION, "USD")),
        lambda: Quantity(10**308, Unit(UnitKind.CURRENCY, Magnitude.BILLION, "USD")),
        lambda: text(" untrimmed", "name"),
        lambda: flags(set()),
        lambda: flags(frozenset({"STALE"})),
        lambda: MeasurementId("EPS"),
        lambda: MeasurementId("earnings.EPS"),
    ],
)
def test_typed_unit_and_identifier_validation(build):
    with pytest.raises(ValidationError):
        build()


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "bad"},
        {"measurement_id": "EPS"},
        {"reference_period": "Q3"},
        {"seasonal_adjustment": "ADJUSTED"},
        {"release_stage": " "},
    ],
)
def test_measurement_context_validation(sample, changes):
    with pytest.raises(ValidationError):
        replace(sample[1].context, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_type": "EARNINGS"},
        {"issuer_id": "ALPHA"},
        {"security_id": "ALPHA"},
    ],
)
def test_event_reference_validation(sample, changes):
    service, row = sample
    ref = service.engine.bind_event(row.context.event_id, PERIOD, 100)
    assert isinstance(ref, EventReference)
    with pytest.raises(ValidationError):
        replace(ref, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "bad"},
        {"provider_source_item_id": " "},
        {"provider_record_id": ""},
        {"source_observation_id": "not-raw"},
        {"source_class": "LICENSED_NEWS"},
        {"available_from_ns": 99},
        {"provider_observed_at_ns": 101},
        {"vintage_at_ns": 101},
    ],
)
def test_provenance_validation(sample, changes):
    with pytest.raises(EIEError):
        replace(sample[1].source, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"context": "invalid"},
        {"source": "invalid"},
        {"revision": "invalid"},
        {"quality_flags": frozenset({"STALE"})},
        {"schema_version": "2.0"},
        {"revision": EventRevision(1, RevisionKind.ORIGINAL, 99)},
        {"value": 2},
        {"estimate_type": "PROVIDER_CONSENSUS_MEAN"},
        {"contributor_id": " analyst "},
        {"contributor_id": "analyst"},
        {"contributor_count": True},
        {"contributor_count": -1},
        {"contributor_count": 1.5},
        {"estimate_type": EstimateKind.PROVIDER_CONSENSUS_COUNT},
        {
            "estimate_type": EstimateKind.PROVIDER_CONSENSUS_COUNT,
            "value": Quantity(-1, Unit(UnitKind.COUNT)),
        },
        {
            "estimate_type": EstimateKind.PROVIDER_CONSENSUS_COUNT,
            "value": Quantity(1.5, Unit(UnitKind.COUNT)),
        },
    ],
)
def test_estimate_observation_validation(sample, changes):
    with pytest.raises(EIEError):
        replace(sample[1], **changes)


def actual_from(row):
    return ActualObservation(
        row.context, row.source, row.value, ActualRole.ACTUAL, 100, row.revision
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"value": 1},
        {"role": "ACTUAL"},
        {"published_at_ns": 101},
        {"previous_reference_period": PERIOD},
        {"role": ActualRole.PREVIOUS},
        {"role": ActualRole.REVISED_PREVIOUS, "previous_reference_period": "Q2"},
    ],
)
def test_actual_validation(sample, changes):
    with pytest.raises(EIEError):
        replace(actual_from(sample[1]), **changes)


def guidance_from(row, at=100, **values):
    source = replace(
        row.source, available_from_ns=at, ingested_at_ns=at, provider_observed_at_ns=at
    )
    return GuidanceObservation(
        replace(row.context, measurement_id=GUIDANCE_EPS),
        ISSUER,
        source,
        EventRevision(1, RevisionKind.ORIGINAL, at),
        at,
        **(values or {"point": row.value}),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"published_at_ns": 101},
        {"issuer_id": "ALPHA"},
        {"point": None},
        {"point": "2"},
        {"low": Quantity(1, Unit(UnitKind.COUNT))},
        {"low": Quantity(3, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD"))},
        {"high": Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD"))},
        {
            "low": Quantity(3, Unit(UnitKind.COUNT)),
            "high": Quantity(1, Unit(UnitKind.COUNT)),
            "point": None,
        },
    ],
)
def test_guidance_validation(sample, changes):
    with pytest.raises(EIEError):
        replace(guidance_from(sample[1]), **changes)


def test_guidance_context_units_ranges_and_comparison_reasons(sample):
    _, row = sample
    with pytest.raises(ValidationError):
        replace(guidance_from(row), context=row.context)
    old = guidance_from(row)
    new = guidance_from(row, 200, point=Quantity(3, row.value.unit))
    assert old.unit == row.value.canonical_unit
    assert old.midpoint == 2.4
    assert old.result_fingerprint
    assert compare_guidance(new, None).reason_codes == ("PRIOR_GUIDANCE_MISSING",)
    assert compare_guidance(old, new).reason_codes == ("PRIOR_GUIDANCE_NOT_PRIOR",)
    different = replace(new, context=replace(new.context, release_stage="ADVANCE"))
    assert compare_guidance(different, old).reason_codes == ("GUIDANCE_CONTEXT_MISMATCH",)
    incompatible = replace(new, point=Quantity(3, Unit(UnitKind.COUNT)))
    assert compare_guidance(incompatible, old).reason_codes == ("INCOMPATIBLE_UNITS",)
    assert compare_guidance(new, old).midpoint_change == pytest.approx(0.6)
    assert compare_guidance(new, old).result_fingerprint
    one_sided = guidance_from(row, 200, low=row.value)
    assert one_sided.midpoint is None
    assert compare_guidance(one_sided, old).midpoint_change is None
    huge_old = replace(old, point=Quantity(-1e308, row.value.unit))
    huge_new = replace(new, point=Quantity(1e308, row.value.unit))
    assert compare_guidance(huge_new, huge_old).reason_codes == ("NUMERIC_OVERFLOW",)


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"allow_relative_surprise": 1},
        {"minimum_history": 2},
        {"minimum_history": True},
        {"max_payload_bytes": 0},
        {"max_items_per_batch": 0},
        {"history_window_events": 2},
        {"history_window_events": True},
        {"normalization_method": "MAD"},
        {"consensus_cutoff_policy": "STRICT_BEFORE"},
        {"provider_pooling_policy": "SAME_PROVIDER"},
        {"staleness_ns": -1},
        {"relative_min_abs_consensus": 0},
        {"relative_min_abs_consensus": float("inf")},
    ],
)
def test_settings_validation(changes):
    with pytest.raises(ValidationError):
        ExpectationsSettings(**changes)


@pytest.mark.parametrize("name", ["scheduled", "official", "sec", "corporate", "expectations"])
def test_root_settings_require_typed_subsettings(name):
    with pytest.raises(ValidationError):
        Settings(**{name: "invalid"})


@pytest.mark.parametrize(
    "changes",
    [
        {"has_vintages": 1},
        {"event_ids": []},
        {"measurements": []},
        {"event_ids": ("bad",)},
        {"event_ids": ("evt_a", "evt_a")},
        {"measurements": ("EPS",)},
        {"measurements": (EPS_DILUTED, EPS_DILUTED)},
    ],
)
def test_coverage_validation(sample, changes):
    coverage = EstimateCoverage("SYNTH_A", (sample[1].context.event_id,), (EPS_DILUTED,), True, 100)
    assert coverage.covers(sample[1].context.event_id, EPS_DILUTED)
    with pytest.raises(ValidationError):
        replace(coverage, **changes)


@pytest.mark.parametrize(
    "observations,asof",
    [
        ([], 100),
        (("invalid",), 100),
        ("future", 99),
        ("duplicate", 100),
    ],
)
def test_history_validation(sample, observations, asof):
    row = sample[1]
    if observations == "future":
        observations = (row,)
    elif observations == "duplicate":
        observations = (row, row)
    with pytest.raises(EIEError):
        ObservationHistory(asof, observations, "config")
    assert ObservationHistory(100, (row,), "config").result_fingerprint
    assert row.estimate_observation_id
    assert row.result_fingerprint
    assert row.source.result_fingerprint
    assert actual_from(row).result_fingerprint


def test_ledger_idempotency_collisions_gaps_and_raw_evidence(sample):
    service, row = sample
    ledger = service.engine.ledger
    assert not ledger.append(row, service.engine.event_store)
    with pytest.raises(PersistenceError, match="collision"):
        ledger.append(replace(row, value=Quantity(9, row.value.unit)), service.engine.event_store)
    gap = replace(row, revision=EventRevision(3, RevisionKind.CORRECTION, 100, 2))
    with pytest.raises(PersistenceError, match="gap"):
        ledger.append(gap, service.engine.event_store)
    same_time = replace(row, revision=EventRevision(2, RevisionKind.CORRECTION, 100, 1))
    with pytest.raises(TemporalConsistencyError, match="advance"):
        ledger.append(same_time, service.engine.event_store)
    assert ledger.history(row.stream_id) == (row,)
    assert ledger.canonical_state_bytes() == canonical_json_bytes((row,))
    with pytest.raises(PersistenceError, match="raw evidence"):
        ObservationLedger().append(row, InMemoryEventStore())
    raw = service.engine.event_store.get_observation(row.source.source_observation_id)
    store = InMemoryEventStore()
    store.append_observation(replace(raw, ingested_at_ns=101))
    with pytest.raises(TemporalConsistencyError, match="predates"):
        ObservationLedger().append(row, store)
    mismatch = replace(
        row, source=replace(row.source, provider_observed_at_ns=99, vintage_at_ns=99)
    )
    with pytest.raises(TemporalConsistencyError, match="clocks"):
        ObservationLedger().append(mismatch, service.engine.event_store)
    mismatch = replace(row, source=replace(row.source, provider_source_item_id="different"))
    with pytest.raises(PersistenceError, match="provenance"):
        ObservationLedger().append(mismatch, service.engine.event_store)


def test_engine_rejects_disabled_unknown_and_incompatible_contexts(sample):
    service, row = sample
    engine = service.engine
    with pytest.raises(ValidationError, match="unresolved"):
        engine.bind_event("evt_" + "f" * 64, PERIOD, 100)
    with pytest.raises(ValidationError, match="period"):
        engine.bind_event(row.context.event_id, MACRO_PERIOD, 100)
    with pytest.raises(ValidationError, match="period"):
        engine.bind_event(row.context.event_id, replace(PERIOD, fiscal_year=2025), 100)
    with pytest.raises(ValidationError, match="not known"):
        engine.consensus(row.context.event_id, EPS_DILUTED, 0)
    engine.settings = Settings()
    with pytest.raises(ValidationError, match="disabled"):
        engine.append(row)
    engine.settings = Settings(expectations=ExpectationsSettings(enabled=True))
    with pytest.raises(ValidationError, match="unresolved"):
        engine.append(replace(row, context=replace(row.context, event_id="evt_" + "f" * 64)))
    with pytest.raises(ValidationError, match="families"):
        engine.append(replace(row, context=replace(row.context, measurement_id=CPI_CORE_MOM)))
    with pytest.raises(ValidationError, match="period"):
        engine.append(replace(row, context=replace(row.context, reference_period=MACRO_PERIOD)))
    with pytest.raises(ValidationError, match="stage"):
        engine.append(replace(row, context=replace(row.context, release_stage="ADVANCE")))
    with pytest.raises(ValidationError, match="issuer"):
        engine.append(replace(guidance_from(row), issuer_id="iss_" + "f" * 64))
    assert engine.health_as_of("UNKNOWN", 100).last_attempt_ns is None


def test_macro_reference_type_and_snapshot_owned_copies():
    service, (event_id,) = seeded(periods=(MACRO_PERIOD,), macro=True)
    with pytest.raises(ValidationError, match="period"):
        service.engine.bind_event(event_id, PERIOD, 100)
    assert service.engine.bind_event(event_id, MACRO_PERIOD, 100).issuer_id is None
