"""Fail-closed validation and schema-drift cases for official-source boundaries."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from eie.core.errors import EIEError, ValidationError
from eie.events.taxonomy import MACRO_CPI_US
from eie.providers.bea.adapter import BEAAdapter, BEACalendarSlot
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.ingestion import IngestionDiagnostics
from eie.providers.common.models import (
    OfficialSnapshot,
    PeriodFrequency,
    ReferencePeriod,
    TimePrecision,
    component_codes,
    load_extract,
    parse_aware_ns,
    parse_date,
    period_from_item,
    schedule_fields,
    validate_source_url,
)
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.federal_reserve.adapter import FederalReserveAdapter
from eie.sources.models import SourceHealth, SourceHealthState

FIXTURES = Path(__file__).parent / "fixtures" / "official"


def loaded(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_bytes())


def blob(value: object) -> bytes:
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    "change",
    [
        {"event_type": "wrong"},
        {"kind": "wrong"},
        {"source_item_id": ""},
        {"title": " bad "},
        {"source_reference": "http://www.bls.gov/unsafe"},
        {"source_reference": "https://evil.example/data"},
        {"source_reference": "https://user:pass@www.bls.gov/data"},
        {"source_reference": "https://www.bls.gov/data?api_key=secret"},
        {"precision": "not-precision"},
        {"precision": TimePrecision.DATE_ONLY},
        {"precision": TimePrecision.UNKNOWN},
        {"scheduled_date": "2026-09-11"},
        {"published_at_ns": -1},
        {"reference_period": "not-period"},
        {"release_stage": ""},
        {"raw_fragment_hash": "abc"},
        {"components": ("CORE_CPI", "CORE_CPI")},
        {"components": ["CORE_CPI"]},
        {"content_type": ""},
        {"source_schema": "2.0"},
        {"source_metadata": (("x", "1"), ("x", "2"))},
    ],
)
def test_official_item_rejects_invalid_contract_fields(change: dict[str, object]) -> None:
    base = BLSAdapter().parse(blob(loaded("bls_schedule.json"))).items[0]
    with pytest.raises(ValidationError):
        replace(base, **change)
    assert base.event_type == MACRO_CPI_US


def test_reference_period_and_time_parser_rejects_unsafe_values() -> None:
    for args in (
        ("bad", "2026-09-01", PeriodFrequency.MONTHLY, "August"),
        ("2026-09-01", "2026-08-01", PeriodFrequency.MONTHLY, "August"),
        ("2026-08-01", "2026-09-01", "MONTHLY", "August"),
        ("2026-08-01", "2026-09-01", PeriodFrequency.MONTHLY, " "),
        ("20260801", "2026-09-01", PeriodFrequency.MONTHLY, "August"),
    ):
        with pytest.raises(ValidationError):
            ReferencePeriod(*args)
    for value in (None, "bad", "2026-09-11T08:30:00"):
        with pytest.raises(ValidationError):
            parse_aware_ns(value, "test")
    for value in (None, "2026-13-01", "2026-9-1"):
        with pytest.raises(ValidationError):
            parse_date(value, "test")
    with pytest.raises(ValidationError):
        schedule_fields({"scheduled_at": "2026-09-11T12:30:00Z", "scheduled_date": "2026-09-11"})
    with pytest.raises(ValidationError):
        period_from_item({"reference_period": []})
    with pytest.raises(ValidationError):
        period_from_item({"reference_period": {"frequency": "YEARLY"}})
    for value in (42, [4], ["CORE_CPI", "CORE_CPI"], ["INVALID"]):
        with pytest.raises(ValidationError):
            component_codes({"components": value}, frozenset({"CORE_CPI"}))
    with pytest.raises(ValidationError):
        validate_source_url("BLS", 5)
    with pytest.raises(ValidationError):
        validate_source_url("UNKNOWN", "https://www.bls.gov/schedule")


def test_snapshot_extract_and_settings_validation() -> None:
    base = BLSAdapter().parse(blob(loaded("bls_schedule.json")))
    with pytest.raises(ValidationError):
        OfficialSnapshot("BLS", "1", (base.items[0], base.items[0]))
    with pytest.raises(ValidationError):
        OfficialSnapshot("BEA", "1", (base.items[0],))
    with pytest.raises(ValidationError):
        OfficialSnapshot("BLS", "1", [base.items[0]])
    with pytest.raises(ValidationError):
        OfficialSnapshot("BLS", "1", (), "2.0")
    with pytest.raises(ValidationError):
        OfficialSnapshot("BLS", "", ())
    for bad in (
        {
            "format": "EIE_OFFICIAL_EXTRACT_V1",
            "provider": "BEA",
            "source_version": "1",
            "items": [],
        },
        {"format": "EIE_OFFICIAL_EXTRACT_V1", "provider": "BLS", "items": []},
        {
            "format": "EIE_OFFICIAL_EXTRACT_V1",
            "provider": "BLS",
            "source_version": "1",
            "items": [3],
        },
    ):
        with pytest.raises(EIEError):
            load_extract(blob(bad), "BLS")
    with pytest.raises(EIEError):
        load_extract(b"\xff", "BLS")
    for change in (
        {"staleness_ns": 0},
        {"max_payload_bytes": 0},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
        {"user_agent": "bad\nheader"},
        {"enabled_ids": ("INVALID",)},
    ):
        with pytest.raises(ValidationError):
            OfficialProviderSettings(**change)
    with pytest.raises(ValidationError):
        IngestionDiagnostics("BLS", 1, -1, 0, 0, 0, 0, SourceHealth(SourceHealthState.HEALTHY))
    with pytest.raises(ValidationError):
        IngestionDiagnostics("", 1, 0, 0, 0, 0, 0, SourceHealth(SourceHealthState.HEALTHY))
    with pytest.raises(ValidationError):
        BEACalendarSlot("UNKNOWN", 1)
    with pytest.raises(ValidationError):
        BEACalendarSlot("GDP", -1)


def test_fed_bls_bea_parser_schema_drift() -> None:
    fed = loaded("fed_calendar.json")
    for mutate in (
        lambda x: x["items"][0].pop("events"),
        lambda x: x["items"][0].pop("meeting_start_date"),
        lambda x: x["items"][0].update({"meeting_start_date": "2026-09-17"}),
        lambda x: x["items"][0].update({"events": ["bad"]}),
        lambda x: x["items"][0].update(
            {"events": [{"event": "UNKNOWN", "status": "SCHEDULE", "scheduled_date": "2026-09-16"}]}
        ),
        lambda x: x["items"][0].update({"events": [{"event": "DECISION", "status": "SCHEDULE"}]}),
    ):
        data = json.loads(json.dumps(fed))
        mutate(data)
        with pytest.raises(ValidationError):
            FederalReserveAdapter().parse(blob(data))
    bls = loaded("bls_schedule.json")
    for mutate in (
        lambda x: x["items"][0].pop("reference_period"),
        lambda x: x["items"][0].update({"release": "UNKNOWN"}),
        lambda x: x["items"][0].pop("scheduled_at"),
        lambda x: x["items"][0].update({"components": ["BAD"]}),
    ):
        data = json.loads(json.dumps(bls))
        mutate(data)
        with pytest.raises(ValidationError):
            BLSAdapter().parse(blob(data))
    bea = loaded("bea_schedule.json")
    for mutate in (
        lambda x: x["items"][0].pop("stage"),
        lambda x: x["items"][0].update({"stage": "FIFTH"}),
        lambda x: x["items"][0].update({"release": "UNKNOWN"}),
        lambda x: x["items"][0].pop("reference_period"),
        lambda x: x["items"][0].pop("scheduled_at"),
    ):
        data = json.loads(json.dumps(bea))
        mutate(data)
        with pytest.raises(ValidationError):
            BEAAdapter().parse(blob(data))
    extra = loaded("bea_schedule.json")
    extra["items"][0]["harmless_extra"] = "ignored by normalizer, retained in raw hash"
    assert len(BEAAdapter().parse(blob(extra)).items) == 3


def test_icalendar_rejects_malformed_critical_structure() -> None:
    base = (
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:one\nSUMMARY:Consumer Price Index for December 2026\n"
        b"DTSTART:20270111T133000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    parsed = BLSAdapter().parse(base)
    assert parsed.items[0].reference_period.end_date == "2027-01-01"
    assert parsed.items[0].scheduled_for_ns is not None
    assert (
        BLSAdapter()
        .parse(base.replace(b"SUMMARY:Consumer Price Index", b"SUMMARY:Unrelated Release"))
        .items
        == ()
    )
    variants = (
        base.replace(b"BEGIN:VCALENDAR\n", b""),
        base.replace(b"BEGIN:VEVENT\n", b"BEGIN:VEVENT\nBEGIN:VEVENT\n"),
        base.replace(b"BEGIN:VEVENT\n", b""),
        base.replace(b"END:VEVENT\n", b""),
        base.replace(b"UID:one\n", b"UID:one\nUID:two\n"),
        base.replace(
            b"SUMMARY:Consumer Price Index for December 2026", b"SUMMARY:Consumer Price Index"
        ),
        base.replace(b"DTSTART:20270111T133000Z", b"DTSTART:bad"),
        base.replace(b"DTSTART:20270111T133000Z", b"DTSTART;TZID=Europe/London:20270111T133000"),
        b"BEGIN:VCALENDAR\nBEGIN:VEVENT\n\xff\nEND:VEVENT\nEND:VCALENDAR\n",
    )
    for bad in variants:
        with pytest.raises(EIEError):
            BLSAdapter().parse(bad)
    folded = base.replace(b"SUMMARY:Consumer Price Index", b"SUMMARY:Consumer Price \n Index")
    assert BLSAdapter().parse(folded).items[0].event_type == MACRO_CPI_US
    date_only = base.replace(b"DTSTART:20270111T133000Z", b"DTSTART;VALUE=DATE:bad")
    with pytest.raises(ValidationError):
        BLSAdapter().parse(date_only)
