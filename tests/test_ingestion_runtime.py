"""Deterministic runtime recovery, queue, scheduler, shutdown and causal scenarios."""

import json
from hashlib import sha256
from threading import Event

import pytest
from test_official_sources import fixture as bls_fixture
from test_official_sources import ns, system
from test_sec_ingestion import T3, URL, fixture, service

from eie.core.errors import PersistenceError, ValidationError
from eie.ingestion.models import (
    Cursor,
    CursorKind,
    FailureClass,
    RetentionMode,
    RetentionPermission,
    RetentionPolicy,
    RuntimeHealthState,
)
from eie.ingestion.processors import AdapterProcessor
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.sec.adapter import SecAdapter
from eie.providers.sec.models import CIK
from eie.runtime.clock import ManualClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.coordinator import IngestionCoordinator
from eie.runtime.providers import ProviderRegistration, bls_provider, sec_provider
from eie.runtime.sources import AcquiredPayload, FixtureSource
from eie.transport.models import HttpResponse
from eie.transport.rate_limit import RatePolicy


def sec(clock, *responses, enabled=True, retention=None):
    source = FixtureSource(
        "SEC_EDGAR",
        "recent",
        responses or (AcquiredPayload(HttpResponse(200, body=fixture("submissions.json"))),),
    )
    processor = AdapterProcessor("sec", service(), SecAdapter(), source_url=URL, clock=clock)
    return ProviderRegistration(
        source, processor, enabled=enabled, retention=retention or RetentionPolicy()
    )


def bls(clock):
    _, seed, _ = system()
    return ProviderRegistration(
        FixtureSource(
            "BLS",
            "calendar",
            (AcquiredPayload(HttpResponse(200, body=bls_fixture("bls_schedule.json"))),),
        ),
        AdapterProcessor("official", seed, BLSAdapter(), clock=clock),
        enabled=True,
    )


def runtime(store, clock, *registrations, **kwargs):
    return IngestionCoordinator(
        store,
        registrations,
        kwargs.pop("config", RuntimeConfig(enabled=True)),
        clock=clock,
        **kwargs,
    )


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary",
    (
        "after_fetch",
        "after_parse",
        "after_prepare",
        "after_raw_preparation",
        "after_persist",
        "before_checkpoint",
        "before_commit",
        "after_commit",
    ),
)
def test_crash_restart_safe_retry(tmp_path, boundary):
    path = tmp_path / "crash.sqlite"
    clock = ManualClock(T3)

    def fail(at):
        if at == boundary:
            raise Crash()

    store = SQLiteEventStore(path)
    coordinator = runtime(store, clock, sec(clock), fault=fail)
    with pytest.raises(Crash):
        coordinator.ingest_once("SEC_EDGAR")
    committed = boundary == "after_commit"
    assert store.counts()["events"] == int(committed)
    assert (store.checkpoint("SEC_EDGAR", "recent") is not None) == committed
    snapshot = store.canonical_state_bytes()
    store.close()
    clock.advance(100)
    with SQLiteEventStore(path) as store:
        coordinator = runtime(store, clock, sec(clock))
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.committed and result.checkpoint_generation == (2 if committed else 1)
        assert store.counts()["events"] == 1
        if committed:
            assert store.canonical_state_bytes() == snapshot and result.duplicates == 1
        assert store.validate() == ()


def test_observed_ingested_published_and_latencies():
    clock = ManualClock(T3)

    def advance(at):
        if at in (
            "after_fetch",
            "before_prepare",
            "after_prepare",
            "after_persist",
            "before_commit",
        ):
            clock.advance(10)

    registration = sec(clock)
    original = registration.source.poll

    def delayed(*args):
        clock.advance(20)
        return original(*args)

    registration.source.poll = delayed
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, registration, fault=advance)
        assert coordinator.fetch_once("SEC_EDGAR") is None
        clock.advance(7)
        result = coordinator.process_next()
        event = store.known_as_of(clock.utc_ns())[0]
        assert result.observed_ns == T3 + 20
        assert event.source_observed_at_ns == T3 + 20 and event.ingested_at_ns == T3 + 47
        assert event.published_at_ns is None
        assert event.effective_at_ns < event.source_observed_at_ns
        assert store.known_as_of(event.ingested_at_ns - 1) == ()
        assert result.latencies.fetch_ns == 20 and result.latencies.queue_ns == 7
        assert result.latencies.prepare_ns == 10 and result.latencies.commit_ns == 20
        assert result.latencies.total_ns == 77


def test_late_out_of_order_and_numeric_resume():
    clock = ManualClock(T3)
    data = json.loads(fixture("submissions.json"))
    recent = data["filings"]["recent"]
    recent["accessionNumber"] = ["0000123456-24-000001"]
    recent["form"] = ["10-K"]
    recent["filingDate"] = ["2024-01-10"]
    recent["acceptanceDateTime"] = ["2024-01-10T14:03:12"]
    payload = json.dumps(data).encode()
    registration = sec(
        clock,
        AcquiredPayload(
            HttpResponse(200, body=fixture("submissions.json")), Cursor(CursorKind.SEQUENCE, "20")
        ),
        AcquiredPayload(HttpResponse(200, body=payload), Cursor(CursorKind.SEQUENCE, "10")),
    )
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, registration)
        assert coordinator.ingest_once("SEC_EDGAR").committed
        old = store.canonical_state_bytes()
        clock.advance(100)
        assert coordinator.ingest_once("SEC_EDGAR").committed
        assert len(store.known_as_of(T3)) == 1 and len(store.known_as_of(T3 + 100)) == 2
        late = next(
            e for e in store.known_as_of(T3 + 100) if e.identity.event_type.code == "SEC.10_K"
        )
        assert late.effective_at_ns < T3 and late.revision.available_from_ns == T3 + 100
        assert store.checkpoint("SEC_EDGAR", "recent").cursor.value == "20"
        assert old != store.canonical_state_bytes()


def test_health_outage_recovery_304_and_staleness(tmp_path):
    clock = ManualClock(T3)
    ok = AcquiredPayload(
        HttpResponse(
            200, (("etag", '"v1"'), ("last-modified", "yesterday")), fixture("submissions.json")
        )
    )
    registration = sec(
        clock,
        ok,
        AcquiredPayload(HttpResponse(404)),
        AcquiredPayload(HttpResponse(429)),
        AcquiredPayload(HttpResponse(304)),
    )
    with SQLiteEventStore(tmp_path / "health.sqlite") as store:
        coordinator = runtime(store, clock, registration)
        assert coordinator.health("SEC_EDGAR").state is RuntimeHealthState.UNKNOWN
        assert coordinator.ingest_once("SEC_EDGAR").committed
        snapshot = store.canonical_state_bytes()
        clock.advance(1)
        assert (
            coordinator.ingest_once("SEC_EDGAR").error_category is FailureClass.SOURCE_UNAVAILABLE
        )
        assert coordinator.health("SEC_EDGAR").state is RuntimeHealthState.UNAVAILABLE
        clock.advance(1)
        assert coordinator.ingest_once("SEC_EDGAR").error_category is FailureClass.RATE_LIMIT
        assert coordinator.health("SEC_EDGAR").state is RuntimeHealthState.RATE_LIMITED
        clock.advance(1)
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.committed and result.items_ingested == 0
        assert store.canonical_state_bytes() == snapshot
        assert coordinator.health("SEC_EDGAR").consecutive_failures == 0
        cp = store.checkpoint("SEC_EDGAR", "recent")
        assert cp.etag == '"v1"' and cp.generation == 2
        clock.advance(301_000_000_000)
        assert coordinator.health("SEC_EDGAR").state is RuntimeHealthState.STALE
        restored = runtime(store, clock, sec(clock))
        assert restored.metrics.snapshot(0)["polls_total"] == 4
        assert restored.health("SEC_EDGAR").state is RuntimeHealthState.STALE


@pytest.mark.parametrize(
    "payload,category",
    ((b"not JSON", FailureClass.PARSE_FAILURE), (b'{"cik":123456}', FailureClass.SCHEMA_MISMATCH)),
)
def test_quarantine_suppresses_normalization_until_manual_clear(payload, category):
    clock = ManualClock(T3)
    registration = sec(clock, AcquiredPayload(HttpResponse(200, body=payload)))
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, registration)
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.error_category is category and result.quarantined
        assert store.checkpoint("SEC_EDGAR", "recent") is None
        original = registration.processor.prepare
        registration.processor.prepare = lambda *args: pytest.fail("quarantine was retried")
        clock.advance(1)
        assert coordinator.ingest_once("SEC_EDGAR").quarantined
        digest = sha256(payload).hexdigest()
        record = store.quarantined(
            "SEC_EDGAR", "recent", digest, registration.processor.adapter_version
        )
        assert record.attempts == 1
        assert store.clear_quarantine(
            "SEC_EDGAR", "recent", digest, registration.processor.adapter_version
        )
        assert not store.clear_quarantine(
            "SEC_EDGAR", "recent", digest, registration.processor.adapter_version
        )
        registration.processor.prepare = original
        clock.advance(1)
        assert coordinator.ingest_once("SEC_EDGAR").quarantined
        assert store.counts()["observations"] == 0


def test_backpressure_and_provider_isolation():
    clock = ManualClock(ns("2026-10-01T00:00:00Z"))
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(
            store,
            clock,
            sec(clock),
            bls(clock),
            config=RuntimeConfig(enabled=True, queue_capacity=1),
        )
        assert coordinator.fetch_once("SEC_EDGAR") is None
        assert coordinator.queue.depth == 1
        with pytest.raises(ValidationError):
            coordinator.fetch_once("SEC_EDGAR")
        rejected = coordinator.fetch_once("BLS")
        assert rejected.error_category is FailureClass.BACKPRESSURE
        assert store.checkpoint("BLS", "calendar") is None
        assert coordinator.process_next().committed
        assert coordinator.process_next() is None
        clock.advance(1)
        assert coordinator.ingest_once("BLS").committed
        assert coordinator.metrics.snapshot(0)["queue_depth"] == 0


def test_scheduler_bounded_workers_and_graceful_shutdown():
    clock = ManualClock(ns("2026-10-01T00:00:00Z"))
    store = SQLiteEventStore(":memory:")
    coordinator = runtime(
        store,
        clock,
        sec(clock),
        bls(clock),
        config=RuntimeConfig(enabled=True, worker_count=2, fetch_count=2),
    )
    done = Event()
    completions = []

    def boundary(at):
        if at == "after_commit":
            completions.append(at)
            if len(completions) == 2:
                done.set()

    coordinator._writer.fault = boundary
    assert coordinator.tick() == ()
    coordinator.start(schedule=False)
    assert coordinator.tick() == ("BLS", "SEC_EDGAR")
    assert coordinator.tick() == ()
    assert done.wait(2)
    assert coordinator.shutdown(close_store=False)
    assert store.counts()["events"] == 4 and store.counts()["checkpoints"] == 2
    assert not any(t.is_alive() for t in coordinator._workers + coordinator._fetchers)
    assert coordinator.health("SEC_EDGAR").state is RuntimeHealthState.STOPPED
    with pytest.raises(ValidationError):
        coordinator.start()
    assert coordinator.shutdown()
    store.close()


def test_forced_shutdown_fences_uncommitted_data():
    clock = ManualClock(T3)
    entered = Event()
    release = Event()

    def blocked(at):
        if at == "after_prepare":
            entered.set()
            release.wait(2)

    store = SQLiteEventStore(":memory:")
    coordinator = runtime(
        store,
        clock,
        sec(clock),
        config=RuntimeConfig(enabled=True, shutdown_timeout=0.01),
        fault=blocked,
    )
    coordinator.start(schedule=False)
    coordinator.tick()
    assert entered.wait(2)
    assert not coordinator.shutdown()
    release.set()
    for thread in coordinator._workers + coordinator._fetchers:
        thread.join(2)
    assert store.counts()["events"] == 0 and store.checkpoint("SEC_EDGAR", "recent") is None
    assert coordinator.shutdown(close_store=False)
    store.close()


def test_persistence_failure_halts_runtime_without_checkpoint(monkeypatch):
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, sec(clock))

        def fail(*args):
            raise PersistenceError("disk failure")

        monkeypatch.setattr(store, "save_session", fail)
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.PERSISTENCE_FAILURE
        assert coordinator.fatal_error is FailureClass.PERSISTENCE_FAILURE
        assert (
            store.counts()["observations"] == 0 and store.checkpoint("SEC_EDGAR", "recent") is None
        )
        with pytest.raises(ValidationError):
            coordinator.ingest_once("SEC_EDGAR")


def test_checkpoint_failure_rolls_back_batch(monkeypatch):
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, sec(clock))

        def fail(*args):
            raise PersistenceError("checkpoint failure")

        monkeypatch.setattr(store, "advance_checkpoint", fail)
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.CHECKPOINT_FAILURE
        assert (
            store.counts()["events"]
            == store.counts()["observations"]
            == store.counts()["sessions"]
            == 0
        )


def test_retention_metadata_default_and_explicit_permission():
    clock = ManualClock(T3)
    for policy, expected in (
        (RetentionPolicy(), None),
        (
            RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED),
            fixture("submissions.json"),
        ),
    ):
        with SQLiteEventStore(":memory:") as store:
            result = runtime(store, clock, sec(clock, retention=policy)).ingest_once("SEC_EDGAR")
            assert result.committed
            assert store._rows("SELECT raw FROM acquisitions")[0][0] == expected
        clock.advance(1)


def test_defaults_validation_and_opt_in():
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store:
        registration = sec(clock)
        with pytest.raises(ValidationError):
            IngestionCoordinator(store, (registration, registration))
        disabled = IngestionCoordinator(store, (registration,), clock=clock)
        with pytest.raises(ValidationError):
            disabled.ingest_once("SEC_EDGAR")
        with pytest.raises(ValidationError):
            disabled.start()
        for provider in ("SEC_EDGAR", "UNKNOWN"):
            with pytest.raises(ValidationError):
                runtime(store, clock, sec(clock, enabled=False)).ingest_once(provider)
    assert not bls_provider(clock).enabled
    assert not sec_provider(CIK.parse(123456), "EIE ops@example.invalid", clock).enabled
    for ua, rate in (
        ("", RatePolicy()),
        ("EIE no-contact", RatePolicy()),
        ("EIE ops@example.invalid", RatePolicy(11, 1)),
    ):
        with pytest.raises(ValidationError):
            sec_provider(CIK.parse(123456), ua, clock, rate=rate)


def test_logging_excludes_payload_and_contact(caplog):
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store, caplog.at_level("INFO", logger="eie.runtime"):
        runtime(store, clock, sec(clock)).ingest_once("SEC_EDGAR")
    assert "provider_poll" in caplog.text
    assert "Alpha" not in caplog.text and "accessionNumber" not in caplog.text
    assert caplog.records[-1].provider == "SEC_EDGAR"
    configured = sec_provider(CIK.parse(123456), "EIE contact@example.invalid", clock)
    assert "contact@example.invalid" not in repr(configured)
