"""Store parity, durable queries, migrations and transaction recovery."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from threading import Thread

import pytest
from expectations_helpers import ISSUER, USD_EPS, adapter, ingest, payload, record, seeded
from test_corporate_ingestion import T3 as CT
from test_corporate_ingestion import adapter as ca
from test_corporate_ingestion import engine as ce
from test_corporate_ingestion import fixture as cf
from test_official_sources import fixture as of
from test_official_sources import ns, system
from test_sec_ingestion import CIK_A, T3, T4, URL, amend_payload, fixture, service

from eie.core.errors import PersistenceError, ValidationError
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import ApplicabilityQuery
from eie.expectations.models import EPS_DILUTED, GUIDANCE_EPS
from eie.ingestion.models import Checkpoint, Cursor, CursorKind
from eie.persistence import codec, migrations, sessions
from eie.persistence.memory import InMemoryEventStore
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.bls.adapter import BLSAdapter
from eie.replay.protocols import StoreReplayReader
from eie.sources.trust import SourceClass


@pytest.fixture(params=("memory", "sqlite"))
def store(request):
    value = InMemoryEventStore() if request.param == "memory" else SQLiteEventStore(":memory:")
    yield value
    if isinstance(value, SQLiteEventStore):
        value.close()


def test_protocol_parity(store, raw, event):
    assert store.get_observation(raw.observation_id) is None
    assert store.get_event(event.event_id, 1000) is None
    assert store.get_revisions(event.event_id) == ()
    with pytest.raises(PersistenceError):
        store.append_event(event)
    assert store.append_observation(raw) and not store.append_observation(raw)
    assert store.append_event(event) and not store.append_event(event)
    second = replace(
        event,
        revision=EventRevision(2, RevisionKind.UPDATE, 210, 1),
        lifecycle=LifecycleState.RELEASED,
        effective_at_ns=200,
    )
    assert store.append_event(second)
    assert store.get_revisions(event.event_id) == (event, second)
    assert store.get_event(event.event_id, 102) is None
    assert store.get_event(event.event_id, 209) == event
    assert store.get_event(event.event_id, 210) == second
    assert store.known_as_of(210, ApplicabilityQuery(country="US"), 190, 201) == (second,)
    assert store.known_as_of(210, ApplicabilityQuery(country="GB")) == ()
    with pytest.raises(PersistenceError):
        store.append_event(replace(second, title="different"))
    with pytest.raises(PersistenceError):
        store.append_observation(replace(raw, ingested_at_ns=102))
    memory = InMemoryEventStore()
    memory.append_observation(raw)
    memory.append_event(event)
    memory.append_event(second)
    assert memory.canonical_state_bytes() == store.canonical_state_bytes()
    assert StoreReplayReader(memory).events_known_at(210) == StoreReplayReader(
        store
    ).events_known_at(210)


def test_reopen_atomic_checkpoint_and_rollback(tmp_path, raw, event):
    path = tmp_path / "state.sqlite"
    cp = Checkpoint("SYNTH_A", "recent", 1, 100, 210, Cursor(CursorKind.OPAQUE, "resume-value"))
    with SQLiteEventStore(path) as db:
        assert db.schema_version == "1.3"
        with pytest.raises(PersistenceError):
            db.advance_checkpoint(cp, 0)
        with pytest.raises(RuntimeError), db.transaction():
            db.append_observation(raw)
            db.append_event(event)
            db.advance_checkpoint(cp, 0)
            raise RuntimeError("before commit")
        assert db.get_observation(raw.observation_id) is None
        assert db.checkpoint("SYNTH_A", "recent") is None
        with db.transaction():
            db.append_observation(raw)
            db.append_event(event)
            db.advance_checkpoint(cp, 0)
        for candidate, expected in ((cp, 0), (replace(cp, generation=3), 1)):
            with pytest.raises(PersistenceError), db.transaction():
                db.advance_checkpoint(candidate, expected)
        with pytest.raises(ValidationError), db.transaction():
            db.advance_checkpoint(replace(cp, generation=2, completed_ns=209), 1)
        assert db.validate() == ()
        snapshot = db.canonical_state_bytes()
    with SQLiteEventStore(path) as db:
        assert db.checkpoint("SYNTH_A", "recent") == cp
        assert db.canonical_state_bytes() == snapshot
        assert not db.append_event(event) and not db.append_observation(raw)
    db.close()
    with pytest.raises(PersistenceError):
        db.get_revisions(event.event_id)


def test_savepoints_foreign_keys_and_thread_ownership(raw, event):
    with SQLiteEventStore(":memory:") as db:
        with db.transaction():
            db.append_observation(raw)
            with pytest.raises(PersistenceError), db.transaction():
                db._connection.execute("INSERT INTO evidence VALUES ('missing',1,'missing')")
            db.append_event(event)
            with pytest.raises(PersistenceError):
                db.close()
            errors = []

            def foreign_thread():
                try:
                    db.advance_checkpoint(Checkpoint("SYNTH_A", "recent", 1, 100, 210), 0)
                except PersistenceError as exc:
                    errors.append(exc)

            thread = Thread(target=foreign_thread)
            thread.start()
            thread.join(1)
            assert not thread.is_alive() and len(errors) == 1
        assert db.get_event(event.event_id, 1000) == event and db.validate() == ()


@pytest.mark.parametrize("kind", ("sec", "corporate", "official", "expectations"))
def test_domain_restart_duplicates_and_fingerprints(tmp_path, kind):
    if kind == "sec":
        original = service()
        original.ingest_ticker_map(fixture("ticker_map.json"), T3 - 1, T3 - 1)
        original.ingest_payload(fixture("submissions.json"), URL, T3, T3)
    elif kind == "corporate":
        original = ce(listings=True, ticker_snapshot=True)
        original.ingest_payload(ca("SYNTH_A"), cf("provider_a"), CT, CT)
    elif kind == "official":
        _, original, _ = system()
        at = ns("2026-10-01T00:00:00Z")
        original.ingest_payload(BLSAdapter(), of("bls_schedule.json"), at, at)
    else:
        original, (event_id,) = seeded()
        ingest(original, [record(event_id, 2.4, vintage=100)], 100)
        ingest(
            original,
            [
                record(
                    event_id,
                    2.5,
                    vintage=None,
                    kind="ACTUAL",
                    record_id="actual",
                    published_at_ns=300,
                )
            ],
            320,
        )
        guide = record(
            event_id,
            None,
            measurement=GUIDANCE_EPS,
            kind="GUIDANCE",
            vintage=None,
            record_id="guidance",
            published_at_ns=400,
            issuer_id=ISSUER,
        )
        guide.pop("quantity")
        guide.update(low={"value": 2.4, "unit": USD_EPS}, high={"value": 2.8, "unit": USD_EPS})
        ingest(original, [guide], 400, source_class=SourceClass.COMPANY_PRIMARY)
    snapshot = sessions.dumps(original)
    path = tmp_path / "domain.sqlite"
    with SQLiteEventStore(path) as db:
        db.save_session(kind, original)
        assert db.counts()["domain_records"] > 0 and db.load_session("missing") is None
    with SQLiteEventStore(path) as db:
        restored = db.load_session(kind)
        assert sessions.dumps(restored) == snapshot
        if kind == "sec":
            assert restored.filings_for_cik(CIK_A, T3) == original.filings_for_cik(CIK_A, T3)
            result = restored.ingest_payload(fixture("submissions.json"), URL, T3 + 1, T3 + 1)
            assert result.raw_created == result.filings_created_or_updated == 0
        elif kind == "corporate":
            assert restored.index.canonical_state_bytes() == original.index.canonical_state_bytes()
            result = restored.ingest_payload(ca("SYNTH_A"), cf("provider_a"), CT + 1, CT + 1)
            assert result.raw_created == result.events_created_or_updated == 0
        elif kind == "official":
            assert restored.index.store.known_as_of(at) == original.index.store.known_as_of(at)
            result = restored.ingest_payload(BLSAdapter(), of("bls_schedule.json"), at + 1, at + 1)
            assert result.raw_created == result.canonical_created == 0
        else:
            for method, measurement, time in (
                ("consensus", EPS_DILUTED, 200),
                ("surprise", EPS_DILUTED, 320),
                ("guidance", GUIDANCE_EPS, 400),
            ):
                assert getattr(restored.engine, method)(event_id, measurement, time) == getattr(
                    original.engine, method
                )(event_id, measurement, time)
            result = restored.ingest_payload(
                adapter(),
                payload([record(event_id, 2.4, vintage=100)], item_id="item-100"),
                500,
                500,
            )
            assert result.observations_created == 0
        before = db.counts()
        db.save_session(kind, restored)
        after = db.counts()
        assert (
            after["events"] == before["events"] and after["observations"] == before["observations"]
        )
        assert db.validate() == ()


@pytest.mark.parametrize("kind", ("unversioned", "future", "gap", "partial"))
def test_schema_fails_closed(tmp_path, kind):
    path = tmp_path / "bad.sqlite"
    with sqlite3.connect(path) as connection:
        if kind == "unversioned":
            connection.execute("CREATE TABLE arbitrary(x)")
        else:
            connection.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, "
                "schema_version TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?)",
                (2 if kind == "gap" else 1, "2.0" if kind == "future" else "1.0"),
            )
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)


def test_migration_failure_rolls_back(monkeypatch):
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(migrations, "MIGRATIONS", (("CREATE TABLE temporary(x)", "invalid SQL"),))
    with pytest.raises(PersistenceError):
        migrations.migrate(connection)
    assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    connection.close()


@pytest.mark.parametrize("path", ("", "file:unsafe", "\x00"))
def test_path_validation(path):
    with pytest.raises(ValidationError):
        SQLiteEventStore(path)


@pytest.mark.parametrize("value", (True, 0, 60001, 1.5))
def test_timeout_validation(value):
    with pytest.raises(ValidationError):
        SQLiteEventStore(":memory:", busy_timeout_ms=value)


@pytest.mark.parametrize(
    "value", (None, True, 2, 2.5, "text", (1, "x"), [1, 2], {("key", 1): {1, 2}}, frozenset({1, 2}))
)
def test_codec_roundtrip(value):
    assert codec.loads(codec.dumps(value)) == value


@pytest.mark.parametrize(
    "value",
    (
        b"not JSON",
        b"[]",
        b'{"type":"os.system","fields":{}}',
        b'{"collection":"bad","items":[]}',
        b'{"collection":"list","items":1}',
        b'{"collection":"dict","items":[[1,2],[1,3]]}',
        b'{"collection":"dict","items":[[1]]}',
        b'{"type":"eie.ingestion.models.Cursor","fields":{}}',
        b'{"enum":"eie.ingestion.models.CursorKind","value":"bad"}',
        b'{"type":"eie.ingestion.models.CursorKind","fields":{}}',
    ),
)
def test_codec_fail_closed(value):
    with pytest.raises(PersistenceError):
        codec.loads(value)


def test_session_versions_fail_closed():
    with SQLiteEventStore(":memory:") as db:
        db.save_session("sec", service())
        db._connection.execute("UPDATE sessions SET layout_version='2.0'")
        with pytest.raises(PersistenceError):
            db.load_session("sec")
    for value in (
        {"state": "os.system", "fields": {}},
        {"state": "eie.providers.sec.adapter.SecIngestor", "fields": {}},
        1,
    ):
        with pytest.raises(PersistenceError):
            sessions.loads(json.dumps(value).encode())
    with pytest.raises(PersistenceError):
        codec.dumps(object())


def test_sec_amendment_and_corporate_conflicts_keep_historical_queries(tmp_path):
    sec_service = service()
    sec_service.ingest_payload(fixture("submissions.json"), URL, T3, T3)
    prior = sec_service.filings_for_cik(CIK_A, T3)
    sec_service.ingest_payload(amend_payload(), URL, T4, T4)
    corporate = ce()
    corporate.ingest_payload(ca("SYNTH_A"), cf("provider_a"), CT, CT)
    corporate_prior = corporate.index.known_as_of(CT)
    corporate.ingest_payload(ca("SYNTH_B"), cf("provider_b"), CT + 1, CT + 1)
    with SQLiteEventStore(tmp_path / "revisions.sqlite") as store:
        store.save_session("sec", sec_service)
        store.save_session("corporate", corporate)
    with SQLiteEventStore(tmp_path / "revisions.sqlite") as store:
        sec_restored = store.load_session("sec")
        assert sec_restored.filings_for_cik(CIK_A, T3) == prior
        assert sec_restored.filings_for_cik(CIK_A, T4) == sec_service.filings_for_cik(CIK_A, T4)
        corporate_restored = store.load_session("corporate")
        assert corporate_restored.index.known_as_of(CT) == corporate_prior
        assert corporate_restored.index.known_as_of(CT + 1) == corporate.index.known_as_of(CT + 1)
        assert store.validate() == ()


@pytest.mark.parametrize("committed", (False, True))
def test_actual_process_exit_recovers_atomic_data_and_checkpoint(tmp_path, raw, event, committed):
    path = tmp_path / "process.sqlite"
    data = tmp_path / "contracts.json"
    data.write_bytes(codec.dumps((raw, event)))
    script = """
import os,sys
from pathlib import Path
from eie.persistence.sqlite import SQLiteEventStore
from eie.persistence.codec import loads
from eie.ingestion.models import Checkpoint
raw,event=loads(Path(sys.argv[2]).read_bytes())
store=SQLiteEventStore(sys.argv[1])
with store.transaction():
    store.append_observation(raw)
    store.append_event(event)
    store.advance_checkpoint(Checkpoint("SYNTH_A","recent",1,100,210),0)
    if sys.argv[3]=="rollback":os._exit(17)
os._exit(17)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), str(data), "commit" if committed else "rollback"],
        timeout=10,
        capture_output=True,
        check=False,
    )
    assert child.returncode == 17, child.stderr.decode()
    with SQLiteEventStore(path) as store:
        assert store.counts()["events"] == int(committed)
        assert store.counts()["observations"] == int(committed)
        assert (store.checkpoint("SYNTH_A", "recent") is not None) == committed
        assert store.validate() == ()
        with store.transaction():
            assert store.append_observation(raw) is not committed
            assert store.append_event(event) is not committed
            if not committed:
                store.advance_checkpoint(Checkpoint("SYNTH_A", "recent", 1, 100, 210), 0)
        assert store.counts()["events"] == 1


def test_corrupted_causal_indexes_fail_closed(raw, event):
    with SQLiteEventStore(":memory:") as store:
        store.append_observation(raw)
        store.append_event(event)
        store._connection.execute("UPDATE events SET available=0")
        with pytest.raises(PersistenceError):
            store.get_event(event.event_id, 100)
        with pytest.raises(PersistenceError):
            store.known_as_of(100)
        assert "INVALID_EVENT_INDEX" in store.validate()
        store._connection.execute(
            "UPDATE observations SET payload=?",
            (codec.dumps(replace(raw, source_item_id="other")),),
        )
        with pytest.raises(PersistenceError):
            store.get_observation(raw.observation_id)
        with store.transaction():
            store.advance_checkpoint(Checkpoint("SYNTH_A", "recent", 1, 1, 1), 0)
        store._connection.execute("UPDATE checkpoints SET resource='altered'")
        with pytest.raises(PersistenceError):
            store.checkpoint("SYNTH_A", "altered")
