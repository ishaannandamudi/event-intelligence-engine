import sqlite3
from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.impact.models import ModelType
from eie.impact.queries import ImpactQueries
from eie.impact.repository import ImpactRepository
from eie.impact.sensitivity import contamination_sensitivity, impact_set
from eie.persistence import codec, migrations
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.repository import StudyRepository
from examples.counterfactual_impact import demo
from tests.impact_helpers import impact, inputs


def test_atomic_models_fits_results_sets_runs_queries_restart(tmp_path):
    store, reader, (base, sector, _) = inputs(tmp_path)
    repo = ImpactRepository(store)
    market, other = impact(base), impact(sector, ModelType.SECTOR_ADJUSTED)
    run = repo.append_run((market, other))
    assert repo.append_run((other, market)) == run
    assert repo.run(run.run_id) == run
    assert not repo.append(market)
    value = impact_set((market, other))
    assert repo.append_set(value) and not repo.append_set(value)
    both = contamination_sensitivity(market)
    assert repo.append_set(both)
    query = ImpactQueries(repo)
    assert query.impact(market.impact_id) == market
    assert query.abnormal_curve(market.impact_id) == market.abnormal_curve
    assert query.car(market.impact_id, "post") == market.cars[0]
    assert query.fit_diagnostics(market.impact_id) == market.fit
    assert query.sensitivity(value.set_id) == value
    assert query.impacts_as_of(market.available_ns, reader.build_id) == ()
    assert market in query.impacts_as_of(
        market.available_ns + 1,
        reader.build_id,
        event_id=market.event_id,
        instrument_id=base.target.identity.instrument_id,
        model_type=ModelType.MARKET_MODEL,
    )
    assert (
        query.impacts_as_of(market.available_ns + 1, reader.build_id, exclude_contaminated=True)
        == ()
    )
    for call in (repo.get, repo.run, repo.sensitivity):
        assert call("unknown") is None
    for call, args in (
        (query.impact, ("unknown",)),
        (query.car, (market.impact_id, "unknown")),
        (query.sensitivity, ("unknown",)),
    ):
        with pytest.raises(ValidationError):
            call(*args)
    for table in ("impact_models", "impact_results", "impact_sets", "impact_runs"):
        for action in ("UPDATE", "DELETE"):
            with pytest.raises(PersistenceError):
                with store.transaction():
                    store._connection.execute(
                        f"UPDATE {table} SET id=id"
                        if action == "UPDATE"
                        else f"DELETE FROM {table}"
                    )
    payload = codec.dumps(market)
    store.close()
    with SQLiteEventStore(tmp_path / "studies.sqlite") as restarted:
        repo = ImpactRepository(restarted)
        assert restarted.schema_version == "1.6"
        assert codec.dumps(repo.get(market.impact_id)) == payload
        assert repo.run(run.run_id) == run
        assert repo.sensitivity(value.set_id) == value


def test_validation_and_run_failure_leave_no_partial_study_model_or_result(tmp_path):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        repo = ImpactRepository(store)
        r = impact(base)
        bad = replace(r, fit=replace(r.fit, missing_pair_count=100))
        with pytest.raises(ValidationError):
            repo.append_run((r, bad))
        assert repo.get(r.impact_id) is None
        assert StudyRepository(store).get(r.inputs.study.study_id) is None
        with pytest.raises(ValidationError):
            repo.append_set(replace(contamination_sensitivity(r), sensitivity=()))
        # Valid local arithmetic cannot assert a fabricated historical fact reference.
        from eie.impact.engine import ImpactEngine
        from examples.counterfactual_impact import CARS, ESTIMATION

        event = replace(
            base.study,
            definition=replace(base.study.definition, historical_fact_id="fact_" + "b" * 64),
        )
        r = ImpactEngine().impact_event(
            replace(base, study=event), ModelType.MARKET_MODEL, ESTIMATION, CARS
        )
        with pytest.raises(PersistenceError):
            repo.append(r)
        assert repo.get(r.impact_id) is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "table,assignment",
    [
        ("impact_models", "anchor=0"),
        ("impact_models", "inputs='bad'"),
        ("impact_results", "available=0"),
        ("impact_results", "fingerprint='bad'"),
        ("impact_models", "fit=x'00'"),
        ("impact_results", "payload=x'00'"),
    ],
)
def test_corrupt_indices_or_typed_payload_fail_closed(tmp_path, table, assignment):
    store, _, (base, _, _) = inputs(tmp_path)
    try:
        repo = ImpactRepository(store)
        r = impact(base)
        repo.append(r)
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute(f"UPDATE {table} SET {assignment}")
        with pytest.raises(PersistenceError):
            repo.get(r.impact_id)
        with pytest.raises(PersistenceError):
            repo.impacts_as_of(r.available_ns + 1)
    finally:
        store.close()


@pytest.mark.parametrize("table,key", [("impact_sets", "study_id"), ("impact_runs", "build_id")])
def test_corrupt_set_or_run_index_fail_closed(tmp_path, table, key):
    store, _, (base, sector, _) = inputs(tmp_path)
    try:
        repo = ImpactRepository(store)
        r, other = impact(base), impact(sector)
        value = impact_set((r, other))
        repo.append_set(value)
        run = repo.append_run(value.results)
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute("PRAGMA foreign_keys=OFF")
        store._connection.execute(f"UPDATE {table} SET {key}='bad'")
        with pytest.raises(PersistenceError):
            repo.sensitivity(value.set_id) if table == "impact_sets" else repo.run(run.run_id)
    finally:
        store.close()


def test_store_1_2_migration_preserves_frozen_build_and_studies_and_rollback(tmp_path, monkeypatch):
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:3])
    store, reader, (base, _, _) = inputs(tmp_path)
    StudyRepository(store).append(base.study)
    build, study = reader.manifest, base.study
    assert store.schema_version == "1.2"
    store.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:3], (*original[3], "INVALID SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(tmp_path / "studies.sqlite")
    connection = sqlite3.connect(tmp_path / "studies.sqlite")
    assert connection.execute(
        "SELECT schema_version FROM schema_migrations ORDER BY version"
    ).fetchall() == [("1.0",), ("1.1",), ("1.2",)]
    assert (
        connection.execute("SELECT name FROM sqlite_master WHERE name='impact_models'").fetchall()
        == []
    )
    connection.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(tmp_path / "studies.sqlite") as upgraded:
        assert upgraded.schema_version == "1.6"
        assert DatasetReader(DatasetRepository(upgraded), build.job.build_id).manifest == build
        assert StudyRepository(upgraded).get(study.study_id) == study
        assert ImpactRepository(upgraded).append(impact(base))


def test_offline_example(tmp_path):
    value = demo(tmp_path)
    assert value["restart_byte_identical"]
    assert len(value["models"]) == 4 and value["model_comparisons"] == 18
