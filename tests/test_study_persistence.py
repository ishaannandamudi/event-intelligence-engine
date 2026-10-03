import sqlite3
from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.persistence import migrations
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.aggregation import DistributionQuery, cohort
from eie.studies.queries import EventStudyFilter, StudyQueries
from eie.studies.repository import StudyRepository
from examples.event_studies import EARNINGS, MINUTE, history
from tests.study_helpers import result


def test_result_definition_run_immutability_restart_and_queries(tmp_path):
    store, reader, ids = history(tmp_path)
    repo = StudyRepository(store)
    values = tuple(result(reader, i, d) for i, d in zip(ids, (3, 5, 7, 9), strict=True))
    run = repo.append_run(values)
    assert repo.run(run.run_id) == run
    assert repo.append_run(tuple(reversed(values))) == run
    assert not repo.append(values[0])
    assert repo.get("unknown") is None and repo.run("unknown") is None
    q = StudyQueries(repo)
    assert q.response_curve(values[0].study_id) == values[0].curve
    assert q.decay(values[0].study_id).peak_response is not None
    query = DistributionQuery(
        values[-1].anchor_ns, reader.build_id, EARNINGS.code, cohort(values[-1]), 2 * MINUTE, 2
    )
    assert q.historical_distribution(query).count == 3
    assert q.class_decay(query).scope == "EVENT_CLASS"
    assert (
        q.studies_as_of(
            values[-1].available_ns + 1,
            reader.build_id,
            EventStudyFilter(exclude_contaminated=True),
        )
        == values[:3]
    )
    assert repo.studies_as_of(values[0].available_ns) == ()
    assert (
        repo.studies_as_of(
            values[0].available_ns + 1,
            build_id=reader.build_id,
            event_id=ids[0],
            instrument_id="security_fixture",
            event_type=EARNINGS.code,
            policy_id=values[0].definition.policy.policy_id,
        )
        == values[:1]
    )
    for call in (q.response_curve, q.decay):
        with pytest.raises(ValidationError):
            call("unknown")
    with pytest.raises(ValidationError):
        repo.append(replace(values[0], post_return=100))
    for table in ("study_definitions", "study_results", "study_runs"):
        for action in ("UPDATE", "DELETE"):
            with pytest.raises(PersistenceError):
                with store.transaction():
                    store._connection.execute(
                        f"{action} {table} SET id=id"
                        if action == "UPDATE"
                        else f"DELETE FROM {table}"
                    )
    fingerprints = tuple(r.result_fingerprint for r in values)
    store.close()
    with SQLiteEventStore(tmp_path / "studies.sqlite") as restarted:
        loaded = StudyRepository(restarted)
        assert loaded.run(run.run_id) == run
        assert (
            tuple(r.result_fingerprint for r in loaded.studies_as_of(values[-1].available_ns + 1))
            == fingerprints
        )
        assert restarted.schema_version == "1.8"


def test_atomic_run_rollback_and_provenance_refusal(tmp_path, monkeypatch):
    store, reader, ids = history(tmp_path)
    try:
        repo = StudyRepository(store)
        r = result(reader, ids[-1])
        bad = replace(r, definition=replace(r.definition, historical_fact_id="fact_" + "b" * 64))
        with pytest.raises(PersistenceError):
            repo.append_run((r, bad))
        assert repo.get(r.study_id) is None
        bad = replace(r, definition=replace(r.definition, historical_fact_id="fact_" + "b" * 64))
        with pytest.raises(PersistenceError):
            repo.append(bad)
        assert repo.get(bad.study_id) is None
        # Same declared market identity with changed supplied content cannot overwrite.
        changed = replace(
            r.market, bars=tuple(replace(b, volume=b.volume + 1) for b in r.market.bars)
        )
        from eie.studies.engine import calculate

        other = calculate(r.definition, changed, r.benchmarks, r.contamination)
        assert other.study_id == r.study_id and other.result_fingerprint != r.result_fingerprint
        repo.append(r)
        with pytest.raises(PersistenceError):
            repo.append(other)
        assert repo.get(r.study_id) == r
    finally:
        store.close()


@pytest.mark.parametrize(
    "table,assignment",
    [
        ("study_definitions", "anchor=0"),
        ("study_definitions", "policy='tampered'"),
        ("study_results", "available=0"),
        ("study_results", "fingerprint='tampered'"),
    ],
)
def test_corrupt_scalar_index_fails_closed(tmp_path, table, assignment):
    store, reader, ids = history(tmp_path)
    try:
        repo = StudyRepository(store)
        r = result(reader, ids[-1])
        repo.append(r)
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute(f"UPDATE {table} SET {assignment}")
        with pytest.raises(PersistenceError):
            repo.get(r.study_id)
        with pytest.raises(PersistenceError):
            repo.studies_as_of(r.available_ns + 1)
    finally:
        store.close()


def test_migration_1_1_to_1_2_and_failed_ddl_rollback(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    for i, migration in enumerate(migrations.MIGRATIONS[:2], 1):
        for sql in migration:
            connection.execute(sql)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?)", (i, migrations.MIGRATION_VERSIONS[i - 1])
        )
    from eie.config.settings import Settings
    from eie.persistence.sessions import dumps
    from eie.providers.sec.adapter import SecIngestor

    old_session = dumps(SecIngestor(Settings()))
    connection.execute("INSERT INTO sessions VALUES ('old','1.0',?)", (old_session,))
    connection.commit()
    connection.close()
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:2], (*original[2], "INVALID SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT * FROM schema_migrations").fetchall() == [
        (1, "1.0"),
        (2, "1.1"),
    ]
    assert connection.execute("SELECT payload FROM sessions WHERE namespace='old'").fetchone() == (
        old_session,
    )
    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE name='study_results'").fetchall()
        == []
    )
    connection.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.8"
        assert store._rows("SELECT payload FROM sessions WHERE namespace='old'") == [(old_session,)]
        assert store.validate() == ()


def test_existing_historical_manifest_survives_additive_store_migration(tmp_path, monkeypatch):
    from eie.archive.storage import FileArchive
    from eie.datasets.models import ValidationStatus
    from eie.datasets.validation import validate_dataset

    old = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", old[:2])
    store, reader, ids = history(tmp_path)
    build_id, original_fingerprint = reader.build_id, reader.manifest.content_fingerprint
    assert reader.manifest.store_schema_version == "1.1"
    store.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", old)
    with (
        SQLiteEventStore(tmp_path / "studies.sqlite") as migrated,
        FileArchive(tmp_path / "archive") as archive,
    ):
        repo = DatasetRepository(migrated)
        manifest = repo.manifest(build_id)
        assert manifest.content_fingerprint == original_fingerprint
        assert migrated.schema_version == "1.8"
        assert validate_dataset(repo, archive, build_id).status is ValidationStatus.VALID
        reader = DatasetReader(repo, build_id)
        r = result(reader, ids[-1])
        assert StudyRepository(migrated).append(r)
