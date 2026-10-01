"""Shared-state concurrency and explicit failure/configuration boundaries."""

import json
from dataclasses import replace
from threading import Barrier, Thread

import pytest
from expectations_helpers import ISSUER, adapter, payload, record, seeded
from test_corporate_ingestion import T3 as CT
from test_corporate_ingestion import adapter as ca
from test_corporate_ingestion import engine as ce
from test_corporate_ingestion import fixture as cf
from test_ingestion_runtime import bls, runtime, sec
from test_sec_ingestion import T3, URL, fixture, service
from test_transport_runtime import Response, transport

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, ValidationError
from eie.expectations.models import EPS_DILUTED
from eie.ingestion.models import (
    FailureClass,
    RetentionMode,
    RetentionPermission,
    RetentionPolicy,
)
from eie.ingestion.processors import AdapterProcessor, IngestionFailure
from eie.persistence import codec, sessions
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.adapter import SecAdapter, SecFailureCategory, SecIngestor
from eie.providers.sec.models import CIK
from eie.providers.sec.settings import SecSettings
from eie.runtime.clock import ManualClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.providers import ProviderRegistration, bls_provider, sec_provider
from eie.runtime.sources import AcquiredPayload, FixtureSource
from eie.transport.errors import AcquisitionCancelled, TransientTransportError
from eie.transport.models import HttpResponse


def test_two_connections_merge_shared_expectations_without_lost_updates(tmp_path):
    path = tmp_path / "shared.sqlite"
    clock = ManualClock(500)
    barrier = Barrier(3)
    seed, (event_id,) = seeded()
    results = []
    errors = []
    stores = [SQLiteEventStore(path), SQLiteEventStore(path)]
    coordinators = []
    for provider, value, store in zip(("SYNTH_A", "SYNTH_B"), (2.4, 2.8), stores, strict=True):
        source = FixtureSource(
            provider,
            "current",
            (
                AcquiredPayload(
                    HttpResponse(
                        200,
                        body=payload(
                            [record(event_id, value, vintage=100)], provider=provider, published=50
                        ),
                    )
                ),
            ),
        )
        processor = AdapterProcessor("expectations", seed, adapter(provider), clock=clock)
        coordinators.append(
            runtime(store, clock, ProviderRegistration(source, processor, enabled=True))
        )

    def ingest(coordinator):
        try:
            barrier.wait(2)
            results.append(coordinator.ingest_once(next(iter(coordinator.providers))))
        except BaseException as exc:
            errors.append(exc)

    threads = [Thread(target=ingest, args=(c,)) for c in coordinators]
    for thread in threads:
        thread.start()
    barrier.wait(2)
    for thread in threads:
        thread.join(3)
    assert not errors and all(r.committed for r in results) and len(results) == 2
    restored = stores[0].load_session("expectations")
    assert len(restored.engine.consensus(event_id, EPS_DILUTED, 500).provider_claims) == 2
    assert restored.engine.consensus(event_id, EPS_DILUTED, 499).provider_claims == ()
    assert stores[0].counts()["observations"] == 3
    assert stores[0]._rows(
        "SELECT DISTINCT issuer FROM domain_records WHERE kind='EventReference'"
    ) == [(ISSUER,)]
    for store in stores:
        store.close()


def test_corporate_bridge_and_operational_config_fingerprint_stability():
    clock = ManualClock(CT)
    outputs = []
    for workers in (1, 3):
        source = FixtureSource(
            "SYNTH_A", "calendar", (AcquiredPayload(HttpResponse(200, body=cf("provider_a"))),)
        )
        processor = AdapterProcessor("corporate", ce(), ca("SYNTH_A"), clock=clock)
        with SQLiteEventStore(":memory:") as store:
            coordinator = runtime(
                store,
                clock,
                ProviderRegistration(source, processor, enabled=True),
                config=RuntimeConfig(enabled=True, worker_count=workers),
            )
            assert coordinator.ingest_once("SYNTH_A").committed
            outputs.append(store.load_session("corporate").index.upcoming_earnings(CT))
    assert outputs[0] == outputs[1]


def test_sec_real_transport_vertical_slice_and_conditionals():
    clock = ManualClock(T3)
    http, connections = transport(
        Response(
            body=fixture("submissions.json"),
            headers={"content-type": "application/json", "etag": '"v1"'},
        ),
        Response(304),
    )
    registration = sec_provider(
        CIK.parse(123456), "EIE ops@example.invalid", clock, enabled=True, transport=http
    )
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, registration)
        assert coordinator.ingest_once("SEC_EDGAR").committed
        assert connections[0].headers["User-Agent"] == "EIE ops@example.invalid"
        clock.advance(1)
        assert coordinator.ingest_once("SEC_EDGAR").committed
        assert connections[1].headers["If-None-Match"] == '"v1"'
        assert store.counts()["events"] == 1
        assert b"ops@example.invalid" not in sessions.dumps(store.load_session("sec"))


def test_bls_native_http_vertical_slice():
    clock = ManualClock(T3)
    ical = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        b"UID:cpi-2026-08@bls.gov\r\nSUMMARY:Consumer Price Index for August 2026\r\n"
        b"DTSTART;TZID=America/New_York:20260911T083000\r\nSEQUENCE:1\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    http, _ = transport(Response(body=ical, headers={"content-type": "text/calendar"}))
    registration = bls_provider(clock, enabled=True, transport=http)
    with SQLiteEventStore(":memory:") as store:
        result = runtime(store, clock, registration).ingest_once("BLS")
        assert result.committed and store.counts()["events"] == 1
        assert store.load_session("official").index.store.known_as_of(T3)


@pytest.mark.parametrize(
    "error",
    (
        TransientTransportError("sensitive failure"),
        AcquisitionCancelled("stop"),
        RuntimeError("provider bug"),
    ),
)
def test_source_failure_isolated_from_other_providers(error, caplog):
    clock = ManualClock(1_791_000_000_000_000_000)
    registration = sec(clock)

    def fail(*args):
        raise error

    registration.source.poll = fail
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, registration, bls(clock))
        assert not coordinator.ingest_once("SEC_EDGAR").committed
        assert coordinator.ingest_once("BLS").committed
        assert "sensitive failure" not in caplog.text


def test_initial_304_backwards_utc_and_payload_bound():
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store:
        result = runtime(store, clock, sec(clock, AcquiredPayload(HttpResponse(304)))).ingest_once(
            "SEC_EDGAR"
        )
        assert result.error_category is FailureClass.CONFIGURATION_FAILURE
        coordinator = runtime(store, clock, sec(clock))
        assert coordinator.ingest_once("SEC_EDGAR").committed
        assert (
            coordinator.ingest_once("SEC_EDGAR").error_category
            is FailureClass.CONFIGURATION_FAILURE
        )
        assert store.checkpoint("SEC_EDGAR", "recent").generation == 1
    with SQLiteEventStore(":memory:") as store:
        limited = replace(sec(clock), max_payload_bytes=1)
        result = runtime(store, clock, limited).ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.CONFIGURATION_FAILURE
        assert store.counts()["events"] == 0


@pytest.mark.parametrize(
    "failure", (ValidationError("normalization failed"), RuntimeError("unexpected failure"))
)
def test_normalization_exception_quarantines_and_rolls_back(failure):
    clock = ManualClock(T3)
    registration = sec(clock)

    def fail(*args, **kwargs):
        raise failure

    registration.processor.prepare = fail
    with SQLiteEventStore(":memory:") as store:
        result = runtime(store, clock, registration).ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.NORMALIZATION_FAILURE and result.quarantined
        assert store.counts()["observations"] == store.counts()["events"] == 0


def test_adapter_diagnostic_normalization_failure(monkeypatch):
    clock = ManualClock(T3)

    def fail(self, payload, url, observed, ingested, **kwargs):
        return self._failure(ingested, SecFailureCategory.NORMALIZATION_ERROR)

    monkeypatch.setattr(SecIngestor, "ingest_payload", fail)
    with SQLiteEventStore(":memory:") as store:
        result = runtime(store, clock, sec(clock)).ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.NORMALIZATION_FAILURE


def test_startup_settings_and_processor_type_fail_closed():
    clock = ManualClock(T3)
    registration = sec(clock)
    with SQLiteEventStore(":memory:") as store:
        assert runtime(store, clock, registration).ingest_once("SEC_EDGAR").committed
        changed = AdapterProcessor(
            "sec",
            SecIngestor(Settings(sec=SecSettings(enabled=True, max_filings_per_ingest=999))),
            SecAdapter(),
            source_url=URL,
        )
        with pytest.raises(ValidationError):
            runtime(store, clock, replace(registration, processor=changed))
    with pytest.raises(IngestionFailure):
        registration.processor.prepare(ce(), b"{}", T3, T3)
    with pytest.raises(ValidationError):
        AdapterProcessor(
            "sec", service(user_agent="private@example.invalid"), SecAdapter(), source_url=URL
        )
    with pytest.raises(ValidationError):
        AdapterProcessor("sec", service(), SecAdapter())
    with pytest.raises(ValidationError):
        AdapterProcessor("sec", SecIngestor(Settings()), SecAdapter(), source_url=URL)
    with pytest.raises(ValidationError):
        replace(registration, enabled="yes")
    with pytest.raises(ValidationError):
        replace(registration, stale_after=1)
    with pytest.raises(ValidationError):
        replace(
            registration,
            retention=RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED, 1),
        )


def test_manual_pending_queue_drains_on_shutdown():
    clock = ManualClock(T3)
    store = SQLiteEventStore(":memory:")
    coordinator = runtime(store, clock, sec(clock))
    assert coordinator.fetch_once("SEC_EDGAR") is None
    assert coordinator.shutdown(close_store=False)
    assert store.counts()["events"] == 1 and coordinator.queue.depth == 0
    store.close()


def test_writers_started_before_first_commit_cannot_mix_normalization_settings(tmp_path):
    path = tmp_path / "settings.sqlite"
    clock = ManualClock(T3)
    first = sec(clock)
    changed = AdapterProcessor(
        "sec",
        SecIngestor(Settings(sec=SecSettings(enabled=True, max_filings_per_ingest=999))),
        SecAdapter(),
        source_url=URL,
        clock=clock,
    )
    with SQLiteEventStore(path) as store_a, SQLiteEventStore(path) as store_b:
        owner_a = runtime(store_a, clock, first)
        owner_b = runtime(store_b, clock, replace(sec(clock), processor=changed))
        assert owner_a.ingest_once("SEC_EDGAR").committed
        original = store_a.canonical_state_bytes()
        clock.advance(1)
        result = owner_b.ingest_once("SEC_EDGAR")
        assert result.error_category is FailureClass.CONFIGURATION_FAILURE
        assert not result.committed and not result.quarantined
        assert store_b.canonical_state_bytes() == original
        assert store_b.checkpoint("SEC_EDGAR", "recent").generation == 1


def test_global_read_or_telemetry_failure_is_visible(monkeypatch):
    clock = ManualClock(T3)
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, sec(clock))
        assert coordinator.fetch_once("SEC_EDGAR") is None

        def fail(*args):
            raise PersistenceError("database unavailable")

        monkeypatch.setattr(store, "checkpoint", fail)
        result = coordinator.process_next()
        assert result.error_category is FailureClass.PERSISTENCE_FAILURE
        assert coordinator.fatal_error is FailureClass.PERSISTENCE_FAILURE
    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, sec(clock))
        monkeypatch.setattr(store, "record_operation", fail)
        result = coordinator.ingest_once("SEC_EDGAR")
        assert result.committed and coordinator.fatal_error is FailureClass.PERSISTENCE_FAILURE
        assert not coordinator.shutdown(close_store=False)


def test_safe_json_and_corrupt_restart_cache_are_detected():
    with pytest.raises(PersistenceError):
        codec.loads(b'{"type":"x","type":"y"}')
    value = json.loads(sessions.dumps(service()))
    value["fields"]["_diagnostics"] = 42
    with pytest.raises(PersistenceError):
        sessions.loads(json.dumps(value).encode())
    for name, state in (
        (
            "eie.providers.estimates.adapter.EstimateIngestor",
            {
                "_engine": 1,
                "_diagnostics": codec.pack([]),
                "_record_ids": codec.pack({}),
                "_attempts": codec.pack({}),
            },
        ),
        (
            "eie.providers.common.ingestion.OfficialSourceIngestor",
            {"settings": codec.pack(Settings()), "index": 1, "_diagnostics": codec.pack({})},
        ),
    ):
        with pytest.raises(PersistenceError):
            sessions.loads(json.dumps({"state": name, "fields": state}).encode())
    with SQLiteEventStore(":memory:") as store:
        store.save_session("sec", service())
        store._connection.execute("UPDATE sessions SET payload=?", (b"invalid JSON",))
        store.record_operation("SEC_EDGAR", 1, "poll", 1)
        store._connection.execute("UPDATE operations SET payload=?", (b"invalid JSON",))
        assert set(store.validate()) == {"INVALID_SESSION", "INVALID_CONTRACT:operations"}


def test_failure_after_commit_cannot_quarantine_committed_data():
    clock = ManualClock(T3)

    def fail(at):
        if at == "after_commit":
            raise RuntimeError("process failed after commit")

    with SQLiteEventStore(":memory:") as store:
        coordinator = runtime(store, clock, sec(clock), fault=fail)
        with pytest.raises(RuntimeError):
            coordinator.ingest_once("SEC_EDGAR")
        assert store.counts()["events"] == 1 and store.counts()["quarantine"] == 0
        assert store.checkpoint("SEC_EDGAR", "recent").generation == 1
