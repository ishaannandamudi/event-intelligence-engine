from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.evidence.claims import TimePrecision, TimeValue
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability import SOURCE_RELIABILITY_SCHEMA_VERSION
from eie.reliability.clocks import exact
from eie.reliability.latency import calculate
from eie.reliability.models import InformationClocks, ProcessingTiming
from eie.reliability.schema import ClockKind as C
from eie.reliability.schema import LatencyQuality as Q
from examples.reliability_fixtures import SECOND, ingest


def test_full_distinct_clock_chain_and_signed_valid_delay():
    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND, publication=6 * SECOND)
        assert SOURCE_RELIABILITY_SCHEMA_VERSION == "1.0"
        latency = calculate(row, 11 * SECOND)
        deltas = {(c.start, c.end): c.delta for c in latency.components}
        assert deltas[C.PUBLICATION_TIME, C.OBSERVED_TIME] == 2 * SECOND
        assert deltas[C.OBSERVED_TIME, C.INGESTED_TIME] == SECOND
        assert deltas[C.INGESTED_TIME, C.NORMALIZED_TIME] == SECOND
        assert deltas[C.NORMALIZED_TIME, C.SNAPSHOT_AVAILABLE_TIME] == SECOND
        assert deltas[C.PUBLICATION_TIME, C.SNAPSHOT_AVAILABLE_TIME] == 5 * SECOND
        assert deltas[C.SOURCE_AVAILABLE_TIME, C.OBSERVED_TIME] is None
        assert Q.MISSING_CLOCK in latency.quality
        assert row.clocks.valid.timestamp_ns == 0


@pytest.mark.parametrize("clock", list(C))
def test_missing_clocks_do_not_become_zero(clock):
    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        clocks = InformationClocks()
        latency = calculate(replace(row, clocks=clocks), 11 * SECOND)
        assert clock in latency.missing_clocks
        assert all(c.delta is None and c.lower is None for c in latency.components)


@pytest.mark.parametrize("precision", [TimePrecision.DATE_ONLY, TimePrecision.DATE_PLUS_SESSION])
def test_coarse_publication_preserves_interval_and_session(precision):
    from eie.corporate.types import ReleaseTiming

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        clock = TimeValue(
            precision,
            "1970-01-01",
            session=ReleaseTiming.BEFORE_MARKET
            if precision is TimePrecision.DATE_PLUS_SESSION
            else ReleaseTiming.UNKNOWN,
        )
        latency = calculate(
            replace(row, clocks=replace(row.clocks, publication=clock, coarse_timezone="UTC")),
            11 * SECOND,
        )
        c = next(
            c
            for c in latency.components
            if (c.start, c.end) == (C.PUBLICATION_TIME, C.OBSERVED_TIME)
        )
        assert c.delta is None and c.lower < 0 < c.upper
        assert Q.COARSE_TIMESTAMP in c.quality and latency.clocks.publication == clock


def test_negative_provider_latency_is_signed_and_qualified():
    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        latency = calculate(
            replace(row, clocks=replace(row.clocks, publication=exact(9 * SECOND))), 11 * SECOND
        )
        c = latency.components[3]
        assert c.delta == -SECOND
        assert {Q.NEGATIVE_LATENCY, Q.CLOCK_SKEW} <= c.quality
        with pytest.raises(ValidationError):
            calculate(row, 9 * SECOND)


def test_processing_reference_must_pin_exact_owner_and_cannot_upgrade_extraction():
    from eie.reliability.inputs import timing_clocks

    timing = ProcessingTiming("subject", "a" * 64, "instrument", 2, exact(1), exact(2))
    with pytest.raises(ValidationError):
        timing_clocks(InformationClocks(), timing, "different", "a" * 64)
    with pytest.raises(ValidationError):
        timing_clocks(InformationClocks(normalized=exact(0)), timing, "subject", "a" * 64)
    with pytest.raises(ValidationError):
        replace(timing, available_ns=0)
    with pytest.raises(ValidationError):
        replace(timing, normalized=None, snapshot=None)


def test_coarse_clock_requires_explicit_timezone_and_handles_dst():
    from eie.reliability.clocks import bounds

    day = TimeValue(TimePrecision.DATE_ONLY, "2026-03-08")
    assert bounds(day) is None
    interval = bounds(day, "America/New_York")
    assert interval[1] - interval[0] + 1 == 23 * 3600 * SECOND
    with pytest.raises(ValidationError):
        InformationClocks(coarse_timezone="invented-zone")
    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        value = calculate(replace(row, clocks=replace(row.clocks, publication=day)), 11 * SECOND)
        assert Q.MISSING_TIMEZONE in value.quality
        assert C.PUBLICATION_TIME not in value.missing_clocks
        assert value.components[3].delta is None
