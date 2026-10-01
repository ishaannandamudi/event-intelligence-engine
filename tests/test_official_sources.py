"""Offline official-source parsing, normalization, health, and causal replay."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from eie.config.settings import Settings
from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.quality import QualityFlag
from eie.core.schema import OFFICIAL_SOURCE_SCHEMA_VERSION
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import RevisionKind
from eie.events.taxonomy import FOMC_DECISION_US
from eie.providers.bea.adapter import BEAAdapter
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.errors import ProviderRateLimitedError, ProviderUnavailableError
from eie.providers.common.ingestion import FailureCategory, OfficialSourceIngestor
from eie.providers.common.models import TimePrecision
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.federal_reserve.adapter import FederalReserveAdapter
from eie.scheduled.engine import ScheduledEventEngine
from eie.scheduled.index import ScheduledEventIndex
from eie.scheduled.models import CoverageStatus, QueryOutcome
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass

FIXTURES = Path(__file__).parent / "fixtures" / "official"


def ns(value: str) -> int:
    return datetime_to_ns(datetime.fromisoformat(value.replace("Z", "+00:00")))


def payload(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def system() -> tuple[ScheduledEventIndex, OfficialSourceIngestor, ScheduledEventEngine]:
    settings = Settings(
        official=OfficialProviderSettings(enabled_ids=("BEA", "BLS", "FEDERAL_RESERVE"))
    )
    index = ScheduledEventIndex(settings)
    return index, OfficialSourceIngestor(index), ScheduledEventEngine(index)


def one_bls() -> dict[str, object]:
    data = json.loads(fixture("bls_schedule.json"))
    data["items"] = [data["items"][0]]
    return data


def test_official_fixtures_parse_distinct_events_and_precision() -> None:
    fed = FederalReserveAdapter().parse(fixture("fed_calendar.json"))
    bls = BLSAdapter().parse(fixture("bls_schedule.json"))
    bea = BEAAdapter().parse(fixture("bea_schedule.json"))
    assert OFFICIAL_SOURCE_SCHEMA_VERSION == "1.0"
    assert len(fed.items) == 4
    assert {item.event_type.code for item in fed.items} == {
        "CENTRAL_BANK.FOMC_DECISION.US",
        "CENTRAL_BANK.FOMC_STATEMENT.US",
        "CENTRAL_BANK.FOMC_SEP.US",
        "CENTRAL_BANK.FOMC_PRESS_CONFERENCE.US",
    }
    assert all(item.precision is TimePrecision.DATE_ONLY for item in fed.items)
    assert all(item.scheduled_for_ns is None for item in fed.items)
    assert {item.event_type.code for item in bls.items} == {
        "MACRO.CPI.US",
        "MACRO.NFP.US",
        "MACRO.PPI.US",
    }
    assert bls.items[0].reference_period is not None
    assert bls.items[0].reference_period.start_date == "2026-08-01"
    assert {item.release_stage for item in bea.items} == {"ADVANCE", "SECOND", None}
    assert len({item.semantic_key for item in bea.items}) == 3
    assert any(
        item.components == ("CORE_PCE", "PERSONAL_INCOME", "PERSONAL_SPENDING")
        for item in bea.items
    )
    assert all(
        item.source_reference.startswith("https://")
        for item in (*fed.items, *bls.items, *bea.items)
    )
    assert all(item.fingerprint == replace(item).fingerprint for item in bls.items)
    with_minutes = json.loads(fixture("fed_calendar.json"))
    with_minutes["items"][0]["events"].append(
        {"event": "MINUTES", "status": "SCHEDULE", "scheduled_date": "2026-10-07"}
    )
    assert "CENTRAL_BANK.FOMC_MINUTES.US" in {
        item.event_type.code for item in FederalReserveAdapter().parse(payload(with_minutes)).items
    }


def test_fomc_release_items_keep_distinct_raw_publication_times() -> None:
    index, ingestor, _ = system()
    data = json.loads(fixture("fed_calendar.json"))
    data["items"][0]["events"] = data["items"][0]["events"][:2]
    events = data["items"][0]["events"]
    for event, published in zip(
        events,
        ("2026-09-16T18:00:00Z", "2026-09-16T18:01:00Z"),
        strict=True,
    ):
        event["status"] = "RELEASE"
        event["published_at"] = published
    observed = ns("2026-09-16T18:02:00Z")
    diagnostic = ingestor.ingest_payload(FederalReserveAdapter(), payload(data), observed, observed)
    assert (diagnostic.raw_created, diagnostic.canonical_created) == (2, 2)
    raw = [
        index.store.get_observation(event.evidence_observation_ids[0])
        for event in index.store.known_as_of(observed)
    ]
    assert all(item is not None for item in raw)
    assert {item.published_at_ns for item in raw if item is not None} == {
        ns("2026-09-16T18:00:00Z"),
        ns("2026-09-16T18:01:00Z"),
    }


def test_fixture_ingestion_provenance_cross_provider_and_idempotency() -> None:
    index, ingestor, engine = system()
    observed = ns("2026-08-01T00:00:00Z")
    fed = ingestor.ingest_payload(
        FederalReserveAdapter(), fixture("fed_calendar.json"), observed, observed
    )
    bls = ingestor.ingest_payload(BLSAdapter(), fixture("bls_schedule.json"), observed, observed)
    bea = ingestor.ingest_payload(BEAAdapter(), fixture("bea_schedule.json"), observed, observed)
    assert (fed.raw_created, fed.canonical_created, fed.schedule_revisions_created) == (4, 4, 0)
    assert (bls.raw_created, bls.canonical_created, bls.schedule_revisions_created) == (3, 3, 3)
    assert (bea.raw_created, bea.canonical_created, bea.schedule_revisions_created) == (3, 3, 3)
    assert len(index.store.known_as_of(observed)) == 10
    assert len(engine.upcoming(observed).events) == 6
    assert all(
        event.source_classes == frozenset({SourceClass.OFFICIAL_PRIMARY})
        for event in index.store.known_as_of(observed)
    )
    assert all(
        index.store.get_observation(event.evidence_observation_ids[0]) is not None
        for event in index.store.known_as_of(observed)
    )
    fed_event = next(
        event
        for event in index.store.known_as_of(observed)
        if event.identity.event_type == FOMC_DECISION_US
    )
    fed_raw = index.store.get_observation(fed_event.evidence_observation_ids[0])
    assert fed_raw is not None
    assert ("fed_meeting_start_date", "2026-09-15") in fed_raw.metadata
    assert ("fed_meeting_end_date", "2026-09-16") in fed_raw.metadata
    assert fed_raw.raw_event_type == FOMC_DECISION_US.code
    fed_raw_ids = {
        event.evidence_observation_ids[0]
        for event in index.store.known_as_of(observed)
        if event.identity.event_type.code.startswith("CENTRAL_BANK.FOMC_")
    }
    assert len(fed_raw_ids) == 4
    first_state = index.canonical_state_bytes()
    later = ingestor.ingest_payload(
        BLSAdapter(), fixture("bls_schedule.json"), observed + 1, observed + 1
    )
    assert (later.raw_created, later.canonical_created, later.duplicates_suppressed) == (0, 0, 3)
    assert index.canonical_state_bytes() == first_state
    assert ingestor.health_as_of("BLS", observed + 1).state is SourceHealthState.HEALTHY
    assert ingestor.health_as_of("BLS", observed - 1).state is SourceHealthState.UNKNOWN


def test_invalid_coverage_does_not_write_official_events() -> None:
    index, ingestor, _ = system()
    observed = ns("2026-09-01T00:00:00Z")
    with pytest.raises(TemporalConsistencyError):
        ingestor.ingest_payload(
            BLSAdapter(),
            payload(one_bls()),
            observed,
            observed,
            coverage_start_ns=observed,
            coverage_end_ns=observed,
        )
    assert index.store.known_as_of(observed) == ()
    assert ingestor.diagnostics("BLS") == ()


def test_historical_schedule_release_correction_and_publication_boundary() -> None:
    index, ingestor, engine = system()
    original_data = one_bls()
    t1 = ns("2026-09-01T00:00:00Z")
    t2 = ns("2026-09-03T00:00:00Z")
    release_time = ns("2026-09-12T08:30:00-04:00")
    t3 = release_time + 60_000_000_000
    t4 = release_time + 3_600_000_000_000
    assert engine.events_known_as_of(t1 - 1).events == ()
    ingestor.ingest_payload(BLSAdapter(), payload(original_data), t1, t1)
    original_view = engine.upcoming(t1).events[0]
    original_time = original_view.scheduled_for_ns
    past = canonical_json_bytes(engine.events_known_as_of(t1))
    revised = deepcopy(original_data)
    revised["items"][0].update(
        {"status": "RESCHEDULE", "item_version": "2", "scheduled_at": "2026-09-12T08:30:00-04:00"}
    )
    ingestor.ingest_payload(BLSAdapter(), payload(revised), t2, t2)
    assert engine.upcoming(t1).events[0].scheduled_for_ns == original_time
    assert engine.upcoming(t2).events[0].scheduled_for_ns == release_time
    event_id = original_view.event_id
    assert engine.resolve_schedule(event_id, t2 - 1).event.revision.kind is RevisionKind.ORIGINAL
    assert engine.resolve_schedule(event_id, t2).event.revision.kind is RevisionKind.RESCHEDULE
    release = deepcopy(revised)
    release["items"][0].update(
        {"status": "RELEASE", "item_version": "3", "published_at": "2026-09-12T08:30:00-04:00"}
    )
    release["items"][0].pop("scheduled_at")
    assert index.store.get_event(event_id, release_time - 1).lifecycle is LifecycleState.RESCHEDULED
    ingestor.ingest_payload(BLSAdapter(), payload(release), release_time, t3)
    assert index.store.get_event(event_id, t3 - 1).lifecycle is LifecycleState.RESCHEDULED
    released = index.store.get_event(event_id, t3)
    assert released.lifecycle is LifecycleState.RELEASED
    assert released.published_at_ns == release_time
    assert released.source_observed_at_ns == release_time
    assert released.ingested_at_ns == t3
    assert released.revision.available_from_ns == t3
    assert len(released.evidence_observation_ids) == 2
    correction = deepcopy(release)
    correction["items"][0].update(
        {
            "status": "CORRECTION",
            "item_version": "4",
            "title": "Corrected CPI for August 2026",
            "published_at": "2026-09-12T09:30:00-04:00",
        }
    )
    ingestor.ingest_payload(BLSAdapter(), payload(correction), t4, t4)
    assert index.store.get_event(event_id, t4 - 1).lifecycle is LifecycleState.RELEASED
    assert index.store.get_event(event_id, t4).lifecycle is LifecycleState.UPDATED
    assert index.store.get_event(event_id, t4).revision.kind is RevisionKind.CORRECTION
    assert canonical_json_bytes(engine.events_known_as_of(t1)) == past
    assert len(index.store.get_revisions(event_id)) == 4


def test_fed_date_only_to_exact_and_release() -> None:
    index, ingestor, engine = system()
    t1 = ns("2026-08-01T00:00:00Z")
    t2 = ns("2026-09-01T00:00:00Z")
    data = json.loads(fixture("fed_calendar.json"))
    ingestor.ingest_payload(FederalReserveAdapter(), payload(data), t1, t1)
    decision = next(
        event
        for event in index.store.known_as_of(t1)
        if event.identity.event_type.code == "CENTRAL_BANK.FOMC_DECISION.US"
    )
    assert decision.scheduled_for_ns is None
    assert engine.upcoming(t1).events == ()
    assert any(
        fact.name == "schedule_date" and fact.value == "2026-09-16" for fact in decision.facts
    )
    exact = deepcopy(data)
    exact["items"][0]["item_version"] = "2"
    exact["items"][0]["events"] = [
        {"event": "DECISION", "status": "SCHEDULE", "scheduled_at": "2026-09-16T14:00:00-04:00"}
    ]
    ingestor.ingest_payload(FederalReserveAdapter(), payload(exact), t2, t2)
    assert engine.resolve_schedule(decision.event_id, t1) is None
    assert engine.resolve_schedule(decision.event_id, t2).event.revision.number == 2
    assert engine.upcoming(t2).events[0].event_id == decision.event_id
    assert index.store.get_event(decision.event_id, t1).scheduled_for_ns is None
    release = deepcopy(exact)
    release["items"][0]["item_version"] = "3"
    release["items"][0]["events"] = [
        {"event": "DECISION", "status": "RELEASE", "published_at": "2026-09-16T14:00:00-04:00"}
    ]
    published = ns("2026-09-16T14:00:00-04:00")
    ingestor.ingest_payload(FederalReserveAdapter(), payload(release), published, published + 1)
    assert index.store.get_event(decision.event_id, published).lifecycle is LifecycleState.SCHEDULED
    assert (
        index.store.get_event(decision.event_id, published + 1).lifecycle is LifecycleState.RELEASED
    )


def test_bea_stages_components_and_cross_provider_simultaneous_order() -> None:
    index, ingestor, engine = system()
    t = ns("2026-08-01T00:00:00Z")
    ingestor.ingest_payload(BEAAdapter(), fixture("bea_schedule.json"), t, t)
    bea_events = index.store.known_as_of(t)
    gdp = [event for event in bea_events if event.identity.event_type.code == "MACRO.GDP.US"]
    assert len(gdp) == 2
    assert gdp[0].event_id != gdp[1].event_id
    assert {
        next(fact.value for fact in event.facts if fact.name == "release_stage") for event in gdp
    } == {"ADVANCE", "SECOND"}
    simultaneous_time = ns("2026-10-29T08:30:00-04:00")
    simultaneous = engine.events_between(simultaneous_time, simultaneous_time + 1, t).events
    assert len(simultaneous) == 2
    assert tuple(view.event_id for view in simultaneous) == tuple(
        sorted(view.event_id for view in simultaneous)
    )
    pce = next(event for event in bea_events if event.identity.event_type.code == "MACRO.PCE.US")
    assert next(fact.value for fact in pce.facts if fact.name == "components") == (
        "CORE_PCE,PERSONAL_INCOME,PERSONAL_SPENDING"
    )


def test_bea_native_machine_calendar_verifies_enriched_extract() -> None:
    adapter = BEAAdapter()
    slots = adapter.parse_machine_calendar(fixture("bea_release_dates.json"))
    snapshot = adapter.parse(fixture("bea_schedule.json"))
    assert len(slots) == 3
    adapter.verify_extract_against_calendar(snapshot, slots)
    assert slots[0].fingerprint == replace(slots[0]).fingerprint
    with pytest.raises(ValidationError):
        adapter.parse_machine_calendar(b"not JSON")
    with pytest.raises(ValidationError):
        adapter.parse_machine_calendar(payload({"Gross Domestic Product": {"wrong": []}}))
    wrong = payload({"Gross Domestic Product": {"release_dates": ["not-a-time"]}})
    with pytest.raises(ValidationError):
        adapter.parse_machine_calendar(wrong)
    with pytest.raises(ValidationError):
        adapter.verify_extract_against_calendar(snapshot, slots[:1])


def test_bea_release_correction_and_stage_specific_identity() -> None:
    index, ingestor, engine = system()
    data = json.loads(fixture("bea_schedule.json"))
    data["items"] = [item for item in data["items"] if item["release_id"] == "GDP-2026Q3-ADVANCE"]
    t1 = ns("2026-09-01T00:00:00Z")
    release_time = ns("2026-10-29T08:30:00-04:00")
    ingestor.ingest_payload(BEAAdapter(), payload(data), t1, t1)
    original = engine.upcoming(t1).events[0]
    release = deepcopy(data)
    release["items"][0].update(
        {"status": "RELEASE", "item_version": "2", "published_at": "2026-10-29T08:30:00-04:00"}
    )
    release["items"][0].pop("scheduled_at")
    ingestor.ingest_payload(BEAAdapter(), payload(release), release_time, release_time + 1)
    assert (
        index.store.get_event(original.event_id, release_time + 1).lifecycle
        is LifecycleState.RELEASED
    )
    correction = deepcopy(release)
    correction["items"][0].update(
        {"status": "CORRECTION", "item_version": "3", "published_at": "2026-10-29T09:00:00-04:00"}
    )
    corrected_at = ns("2026-10-29T09:00:00-04:00")
    ingestor.ingest_payload(BEAAdapter(), payload(correction), corrected_at, corrected_at)
    assert (
        index.store.get_event(original.event_id, corrected_at - 1).lifecycle
        is LifecycleState.RELEASED
    )
    assert (
        index.store.get_event(original.event_id, corrected_at).revision.kind
        is RevisionKind.CORRECTION
    )
    assert engine.resolve_schedule(original.event_id, t1).event.revision.number == 1
    assert engine.resolve_schedule(original.event_id, corrected_at).event.revision.number == 3


def test_official_disagreement_is_flagged_and_prior_time_preserved() -> None:
    index, ingestor, engine = system()
    first = one_bls()
    t1, t2 = ns("2026-09-01T00:00:00Z"), ns("2026-09-02T00:00:00Z")
    ingestor.ingest_payload(BLSAdapter(), payload(first), t1, t1)
    original = engine.upcoming(t1).events[0]
    conflicting = deepcopy(first)
    conflicting["items"][0].update(
        {"item_version": "2", "scheduled_at": "2026-09-12T08:30:00-04:00"}
    )
    ingestor.ingest_payload(BLSAdapter(), payload(conflicting), t2, t2)
    resolved = engine.resolve_schedule(original.event_id, t2)
    assert resolved.event.scheduled_for_ns == original.scheduled_for_ns
    assert QualityFlag.CONFLICTING_SOURCES in resolved.event.quality_flags
    assert len(resolved.claims) == 2
    assert resolved.adjudication_reason_codes == ("PRESERVE_PRIOR_PENDING_REVIEW",)


def test_official_schedule_correction_and_cancellation_replay() -> None:
    index, ingestor, engine = system()
    original = one_bls()
    t1, t2, t3 = (ns(f"2026-09-0{day}T00:00:00Z") for day in (1, 2, 3))
    ingestor.ingest_payload(BLSAdapter(), payload(original), t1, t1)
    first = engine.upcoming(t1).events[0]
    corrected = deepcopy(original)
    corrected["items"][0].update(
        {
            "status": "SCHEDULE_CORRECTION",
            "item_version": "2",
            "scheduled_at": "2026-09-11T10:00:00-04:00",
        }
    )
    ingestor.ingest_payload(BLSAdapter(), payload(corrected), t2, t2)
    assert (
        engine.resolve_schedule(first.event_id, t2).event.revision.kind is RevisionKind.CORRECTION
    )
    assert engine.resolve_schedule(first.event_id, t2).event.lifecycle is LifecycleState.SCHEDULED
    assert engine.upcoming(t1).events[0].scheduled_for_ns == first.scheduled_for_ns
    cancelled = deepcopy(corrected)
    cancelled["items"][0].update({"status": "CANCELLATION", "item_version": "3"})
    cancelled["items"][0].pop("scheduled_at")
    ingestor.ingest_payload(BLSAdapter(), payload(cancelled), t3, t3)
    assert engine.upcoming(t2).events[0].scheduled_for_ns == ns("2026-09-11T10:00:00-04:00")
    assert engine.upcoming(t3).events == ()
    final = engine.resolve_schedule(first.event_id, t3)
    assert final.event.lifecycle is LifecycleState.CANCELLED
    assert final.claims[0].proposed_time_ns is None


def test_release_without_prior_schedule_and_missing_publication_quality() -> None:
    index, ingestor, engine = system()
    data = one_bls()
    data["items"][0]["status"] = "RELEASE"
    data["items"][0].pop("scheduled_at")
    t = ns("2026-09-11T08:31:00-04:00")
    ingestor.ingest_payload(BLSAdapter(), payload(data), t, t)
    event = index.store.known_as_of(t)[0]
    assert event.lifecycle is LifecycleState.RELEASED
    assert event.scheduled_for_ns is None
    assert event.published_at_ns is None
    assert QualityFlag.MISSING_TIMESTAMP in event.quality_flags
    assert engine.upcoming(t).events == ()


def test_parser_drift_empty_health_outage_and_staleness() -> None:
    index, ingestor, engine = system()
    t = ns("2026-08-01T00:00:00Z")
    low, high = ns("2027-01-01T00:00:00Z"), ns("2027-02-01T00:00:00Z")
    empty = {
        "format": "EIE_OFFICIAL_EXTRACT_V1",
        "provider": "BLS",
        "source_version": "empty-calendar",
        "items": [],
        "harmless_extra": True,
    }
    healthy = ingestor.ingest_payload(
        BLSAdapter(), payload(empty), t, t, coverage_start_ns=low, coverage_end_ns=high
    )
    assert healthy.items_seen == 0 and healthy.health.state is SourceHealthState.HEALTHY
    assert engine.events_between(low, high, t).outcome is QueryOutcome.NO_MATCH
    bad = ingestor.ingest_payload(
        BLSAdapter(), b"{bad json", t + 1, t + 1, coverage_start_ns=low, coverage_end_ns=high
    )
    assert bad.error_category is FailureCategory.PARSE_ERROR
    assert bad.health.parse_failures == 1
    assert engine.events_between(low, high, t + 1).outcome is QueryOutcome.INDETERMINATE
    changed = ingestor.ingest_payload(
        BLSAdapter(),
        b'{"schema":"unknown"}',
        t + 2,
        t + 2,
        coverage_start_ns=low,
        coverage_end_ns=high,
    )
    assert changed.health.state is SourceHealthState.SCHEMA_MISMATCH
    assert engine.events_between(low, high, t + 2).coverage.status is CoverageStatus.UNAVAILABLE
    missing = one_bls()
    del missing["items"][0]["release_id"]
    assert (
        ingestor.ingest_payload(BLSAdapter(), payload(missing), t + 3, t + 3).error_category
        is FailureCategory.SCHEMA_MISMATCH
    )
    outage = ingestor.report_failure(BLSAdapter(), t + 4, FailureCategory.UNAVAILABLE)
    assert outage.health.state is SourceHealthState.UNAVAILABLE
    assert (
        ingestor.report_failure(BEAAdapter(), t + 4, FailureCategory.RATE_LIMITED).health.state
        is SourceHealthState.RATE_LIMITED
    )
    stale_index, stale_ingestor, _ = system()
    later = t + stale_index.settings.official.staleness_ns + 10
    stale_ingestor.ingest_payload(
        BEAAdapter(),
        payload(
            {
                "format": "EIE_OFFICIAL_EXTRACT_V1",
                "provider": "BEA",
                "source_version": "empty",
                "items": [],
            }
        ),
        t,
        t,
    )
    assert stale_ingestor.health_as_of("BEA", later).state is SourceHealthState.STALE


def test_one_official_provider_failure_preserves_other_healthy_coverage() -> None:
    _, ingestor, engine = system()
    t = ns("2026-08-01T00:00:00Z")
    low, high = ns("2027-01-01T00:00:00Z"), ns("2027-02-01T00:00:00Z")
    for provider_id, adapter in (("BLS", BLSAdapter()), ("BEA", BEAAdapter())):
        empty = {
            "format": "EIE_OFFICIAL_EXTRACT_V1",
            "provider": provider_id,
            "source_version": "empty",
            "items": [],
        }
        ingestor.ingest_payload(
            adapter, payload(empty), t, t, coverage_start_ns=low, coverage_end_ns=high
        )
    assert engine.events_between(low, high, t).coverage.status is CoverageStatus.AVAILABLE
    ingestor.report_failure(
        BLSAdapter(),
        t + 1,
        FailureCategory.UNAVAILABLE,
        coverage_start_ns=low,
        coverage_end_ns=high,
    )
    result = engine.events_between(low, high, t + 1)
    assert result.coverage.status is CoverageStatus.DEGRADED
    assert result.outcome is QueryOutcome.INDETERMINATE
    assert result.coverage.source_states == (
        ("BEA", SourceHealthState.HEALTHY),
        ("BLS", SourceHealthState.UNAVAILABLE),
    )


def test_bls_native_calendar_timezone_dst_and_malformed_fields() -> None:
    ical = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:cpi-2026-08@bls.gov\r
SUMMARY:Consumer Price Index for August 2026\r
DTSTART;TZID=America/New_York:20260911T083000\r
SEQUENCE:1\r
X-EXTRA:ignored\r
END:VEVENT\r
END:VCALENDAR\r
"""
    parsed = BLSAdapter().parse(ical)
    assert parsed.items[0].scheduled_for_ns == ns("2026-09-11T08:30:00-04:00")
    assert parsed.items[0].reference_period.start_date == "2026-08-01"
    assert BLSAdapter().parse(ical.replace(b"20260911T083000", b"20260308T083000")).items[
        0
    ].scheduled_for_ns == ns("2026-03-08T08:30:00-04:00")
    with pytest.raises(ValidationError):
        BLSAdapter().parse(ical.replace(b"UID:cpi-2026-08@bls.gov\r\n", b""))
    with pytest.raises(ValidationError):
        BLSAdapter().parse(ical.replace(b"20260911T083000", b"20260308T023000"))
    date_only = ical.replace(
        b"DTSTART;TZID=America/New_York:20260911T083000", b"DTSTART;VALUE=DATE:20260911"
    )
    assert BLSAdapter().parse(date_only).items[0].precision is TimePrecision.DATE_ONLY


def test_config_validation_and_no_implicit_provider_access() -> None:
    assert Settings().official.enabled_ids == ()
    assert (
        Settings().fingerprint
        != Settings(official=OfficialProviderSettings(enabled_ids=("BLS",))).fingerprint
    )
    index = ScheduledEventIndex(Settings())
    with pytest.raises(ValidationError):
        OfficialSourceIngestor(index).ingest_payload(
            BLSAdapter(), fixture("bls_schedule.json"), 1, 1
        )
    with pytest.raises(ValidationError):
        OfficialProviderSettings(enabled_ids=("BLS", "BEA"))
    with pytest.raises(TemporalConsistencyError):
        system()[1].ingest_payload(BLSAdapter(), fixture("bls_schedule.json"), 2, 1)
    with pytest.raises(ValidationError):
        BLSAdapter().parse(
            payload(
                {
                    "format": "EIE_OFFICIAL_EXTRACT_V1",
                    "provider": "BLS",
                    "source_version": "1",
                    "items": [{"release": "UNKNOWN"}],
                }
            )
        )


def test_repeated_fixture_builds_are_byte_identical() -> None:
    states: list[bytes] = []
    replays: list[bytes] = []
    for order in (
        ("bls_schedule.json", "bea_schedule.json"),
        ("bea_schedule.json", "bls_schedule.json"),
    ):
        index, ingestor, engine = system()
        t = ns("2026-08-01T00:00:00Z")
        for name in order:
            adapter = BLSAdapter() if name.startswith("bls") else BEAAdapter()
            ingestor.ingest_payload(adapter, fixture(name), t, t)
        states.append(index.canonical_state_bytes())
        replays.append(canonical_json_bytes(engine.upcoming(t)))
    assert states[0] == states[1]
    assert replays[0] == replays[1]


def test_explicit_transport_boundary_and_failure_categories() -> None:
    class FixtureTransport:
        def __init__(self, body: bytes | None) -> None:
            self.body = body
            self.calls: list[tuple[str, float, str]] = []

        def get(self, url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
            self.calls.append((url, timeout_seconds, user_agent))
            if self.body is None:
                raise ProviderUnavailableError("offline fixture outage")
            return self.body

    class LimitedTransport(FixtureTransport):
        def get(self, url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
            raise ProviderRateLimitedError("fixture rate limit")

    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    t = ns("2026-08-01T00:00:00Z")
    index, ingestor, _ = system()
    transport = FixtureTransport(fixture("bls_schedule.json"))
    result = ingestor.fetch_and_ingest(BLSAdapter(), url, transport, t, t)
    assert result.items_seen == 3
    assert transport.calls == [(url, 10, index.settings.official.user_agent)]
    unavailable = ingestor.fetch_and_ingest(BLSAdapter(), url, FixtureTransport(None), t + 1, t + 1)
    assert unavailable.health.state is SourceHealthState.UNAVAILABLE
    limited = ingestor.fetch_and_ingest(BLSAdapter(), url, LimitedTransport(None), t + 2, t + 2)
    assert limited.health.state is SourceHealthState.RATE_LIMITED
    with pytest.raises(ValidationError):
        ingestor.fetch_and_ingest(
            BLSAdapter(), "https://evil.example/data", transport, t + 3, t + 3
        )
    with pytest.raises(ValidationError):
        ingestor.fetch_and_ingest(
            BLSAdapter(), "https://user:pass@www.bls.gov/data", transport, t + 3, t + 3
        )
    disabled = OfficialSourceIngestor(ScheduledEventIndex(Settings()))
    with pytest.raises(ValidationError):
        disabled.fetch_and_ingest(BLSAdapter(), url, transport, t, t)
    assert len(transport.calls) == 1
