import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from threading import Thread

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.schema import ClaimStatus
from eie.persistence import migrations
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.models import ReliabilityQuery, SourceReliabilityPolicy
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, SECOND, ingest


def q(cutoff=50 * SECOND):
    return ReliabilityQuery(DATASET.dataset_id, "A", "financial", cutoff, SourceReliabilityPolicy())


def test_restart_bytes_immutable_artifacts_and_index_corruption(tmp_path):
    path = tmp_path / "reliability.sqlite"
    with SQLiteEventStore(path) as store:
        _, row = ingest(store, "A", 10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        result = engine.source_reliability(q())
        saved = dumps(result)
        audit = engine.audit(DATASET.dataset_id, row.subject_id, 50 * SECOND, 0)
        latency = engine.latency(row.artifact_id, 50 * SECOND)
        for table in (
            "reliability_rows",
            "reliability_artifacts",
            "reliability_requests",
            "reliability_seals",
        ):
            with pytest.raises(PersistenceError):
                with store.transaction():
                    store._connection.execute(f"DELETE FROM {table}")
    with SQLiteEventStore(path) as store:
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        assert dumps(engine.source_reliability(q())) == saved
        validate(repo, result, deep=True)
        validate(repo, audit, deep=True)
        validate(repo, latency, deep=True)
        store._connection.execute("DROP TRIGGER immutable_reliability_rows_update")
        store._connection.execute("UPDATE reliability_rows SET available=available+1")
        with pytest.raises(PersistenceError):
            repo.row(row.artifact_id)


def test_migration_19_to_20_is_transactional_and_preserves_sources(tmp_path, monkeypatch):
    original = migrations.MIGRATIONS
    path = tmp_path / "old.sqlite"
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:10])
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.9"
        store._connection.execute("INSERT INTO sessions VALUES ('old','1.0',?)", (b"frozen",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:10], (*original[10], "invalid SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT schema_version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone() == ("1.9",)
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='reliability_rows'"
    ).fetchall()
    conn.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "2.0"
        assert store._rows("SELECT payload FROM sessions WHERE namespace='old'") == [(b"frozen",)]


def test_atomic_run_rolls_back_on_output_bound(tmp_path):
    with SQLiteEventStore(tmp_path / "run.sqlite") as store:
        ingest(store, "A", 10 * SECOND)
        engine = ReliabilityQueries(ReliabilityRepository(store))
        limited = replace(q(), policy=replace(q().policy, max_bytes=1))
        with pytest.raises(ValidationError):
            engine.run((q(), limited))
        assert not store._rows("SELECT * FROM reliability_requests")
        assert not store._rows("SELECT * FROM reliability_seals")
        run = engine.run((q(), q()))
        assert len(run.result_ids) == 1


def test_future_ingestion_waits_for_historical_transaction(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    with SQLiteEventStore(path) as store:
        first, _ = ingest(store, "A", 10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        errors = []

        def later():
            try:
                with SQLiteEventStore(path) as other:
                    ingest(other, "A", 100 * SECOND, prior=first, status=ClaimStatus.UPDATED)
            except Exception as exc:
                errors.append(exc)

        with store.transaction():
            thread = Thread(target=later)
            thread.start()
            frozen = engine.source_reliability(q())
            assert frozen.sample_count == 1
        thread.join(timeout=10)
        assert not thread.is_alive() and not errors
        assert dumps(engine.source_reliability(q())) == dumps(frozen)
        validate(repo, frozen, deep=True)


def test_admissible_insertion_order_and_hash_seeds_are_deterministic():
    outputs = []
    for order in (("first", "second"), ("second", "first")):
        with SQLiteEventStore(":memory:") as store:
            for key in order:
                ingest(store, "A", 10 * SECOND, key=key)
            outputs.append(
                dumps(ReliabilityQueries(ReliabilityRepository(store)).source_reliability(q()))
            )
    assert outputs[0] == outputs[1]
    script = """from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.models import ReliabilityQuery,SourceReliabilityPolicy
from examples.reliability_fixtures import ingest,DATASET,SECOND
with SQLiteEventStore(':memory:') as store:
 for key in {'first','second'}: ingest(store,'A',10*SECOND,key=key)
 print(ReliabilityQueries(ReliabilityRepository(store)).source_reliability(
  ReliabilityQuery(DATASET.dataset_id,'A','financial',50*SECOND,SourceReliabilityPolicy())).fingerprint)
"""
    env = dict(os.environ, PYTHONPATH="src:.:tests", PYTHONDONTWRITEBYTECODE="1")
    results = [
        subprocess.check_output(
            [sys.executable, "-c", script], env=dict(env, PYTHONHASHSEED=str(seed))
        )
        for seed in (1, 2, 99)
    ]
    assert len(set(results)) == 1
