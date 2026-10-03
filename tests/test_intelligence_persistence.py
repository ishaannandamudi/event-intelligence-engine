"""Atomic snapshots, additive migrations, append-only artifacts, and corruption refusal."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRunManifest
from eie.intelligence.policy import IntelligenceLimits
from eie.intelligence.queries import IntelligenceQueries
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.schema import (
    MaterialityCategory,
    NoveltyDimension,
    UncertaintyKind,
    UncertaintyState,
)
from eie.intelligence.validation import validate
from eie.persistence import migrations
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import DATASET, ISSUER, batch, source
from tests.intelligence_helpers import compute, ingest, request


def test_queries_runs_restart_and_current_versions(tmp_path):
    path = tmp_path / "intelligence.sqlite"
    with SQLiteEventStore(path) as store:
        p = source(family="origin")
        b = batch(p, 20)
        ingest(store, p, b)
        result = compute(store, b)
        repo = IntelligenceRepository(store)
        query = IntelligenceQueries(repo, DATASET.dataset_id)
        run = IntelligenceRunManifest(
            (result.intelligence_id,),
            (result.result_fingerprint,),
            DATASET.dataset_id,
            result.manifest.request.policy_id,
            30,
        )
        repo.append_run(run)
        repo.append_run(run)
        assert repo.run(run.run_id) == run
        assert (
            repo.get("missing") is None
            and repo.audit("missing") is None
            and repo.run("missing") is None
        )
        assert validate(repo, "missing").state.value == "INVALID"
        assert query.intelligence_as_of(result.event_id, 19) is None
        assert (
            query.novelty(result.event_id, 19) == ()
            and query.uncertainty(result.event_id, 19) == ()
        )
        assert query.materiality(result.event_id, 19) is None
        assert query.intelligence_as_of(result.event_id, 20) == result
        assert query.materiality(result.event_id, 20) == result.materiality
        assert query.uncertainty(result.event_id, 20) == result.uncertainty
        assert query.novelty(
            result.event_id, 20, NoveltyDimension.NUMERIC
        ) == query.prior_distribution(result.event_id, 20)
        assert query.history(
            20,
            event_id=result.event_id,
            family="EARNINGS",
            event_type="EARNINGS.RELEASE",
            entity_id=ISSUER,
            source_family="origin",
            measurement="EARNINGS.REVENUE",
            policy_id=result.manifest.request.policy_id,
            materiality=MaterialityCategory.MAJOR,
            uncertainty_kind=UncertaintyKind.IMPACT,
            uncertainty_state=UncertaintyState.NOT_COMPUTED,
        ) == (result,)
        assert query.history(20, entity_id="missing") == ()
        assert query.history(20, measurement="EARNINGS.EPS") == ()
        assert query.prior_distribution(result.event_id, 20, "missing") == ()
        with pytest.raises(ValidationError):
            query.history(20, start_ns=21)
        with pytest.raises(ValidationError):
            query.history(20, uncertainty_kind=UncertaintyKind.SOURCE)
        repo.append(result)
        with pytest.raises(ValidationError):
            repo.append(replace(result, quality=("FABRICATED",)))
        saved = dumps(result)
        identity = result.intelligence_id
        run_id = run.run_id
    with SQLiteEventStore(path) as store:
        repo = IntelligenceRepository(store)
        assert store.schema_version == "1.9" and dumps(repo.get(identity)) == saved
        assert repo.run(run_id) == run and validate(repo, identity).state.value != "INVALID"


def test_same_time_insert_order_and_concurrent_different_event_snapshots():
    fingerprints = []
    for reverse in (False, True):
        with SQLiteEventStore(":memory:") as store:
            p = source()
            a, b = batch(p, 20, key="first"), batch(p, 20, key="second")
            for row in (b, a) if reverse else (a, b):
                ingest(store, p, row)
            repo = IntelligenceRepository(store)
            reqs = tuple(request(repo, row) for row in (a, b))
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(IntelligenceEngine(repo).compute, reqs))
            assert all(r.manifest.prior == () for r in results)
            assert all(
                len([ref for ref in r.manifest.current if ref.kind.value == "ASSERTION"]) == 1
                for r in results
            )
            fingerprints.append(tuple(r.result_fingerprint for r in results))
    assert fingerprints[0] == fingerprints[1]


@pytest.mark.parametrize(
    "table,column",
    [
        ("ei_policies", "fingerprint"),
        ("ei_inputs", "fingerprint"),
        ("ei_results", "family"),
        ("ei_novelty", "state"),
        ("ei_materiality", "category"),
        ("ei_uncertainty", "state"),
        ("ei_entities", "entity"),
        ("ei_sources", "family"),
        ("ei_measurements", "measurement"),
    ],
)
def test_immutability_and_index_corruption_are_detected(table, column):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        r = compute(store, b)
        repo = IntelligenceRepository(store)
        for action in ("UPDATE", "DELETE"):
            with pytest.raises(PersistenceError), store.transaction():
                store._connection.execute(
                    f"UPDATE {table} SET {column}={column}"
                    if action == "UPDATE"
                    else f"DELETE FROM {table}"
                )
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute(f"UPDATE {table} SET {column}=?", ("CORRUPTED",))
        with pytest.raises(PersistenceError):
            repo.get(r.intelligence_id)
        assert validate(repo, r.intelligence_id).state.value == "INVALID"


@pytest.mark.parametrize("table", ["ei_policies", "ei_inputs", "ei_results"])
def test_payload_corruption_fails_closed(table):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        r = compute(store, b)
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute(f"UPDATE {table} SET payload=?", (b"not JSON",))
        assert validate(IntelligenceRepository(store), r.intelligence_id).state.value == "INVALID"


def test_recalculation_detects_coherent_but_forged_component_and_rolls_back_bounds():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        repo = IntelligenceRepository(store)
        req = request(repo, b)
        for limits in (IntelligenceLimits(components=1), IntelligenceLimits(uncertainty_reasons=1)):
            with pytest.raises(ValidationError):
                IntelligenceEngine(repo, limits=limits).compute(req)
            assert (
                store._rows("SELECT id FROM ei_results") == []
                and store._rows("SELECT known FROM ce_seals") == []
            )
        result = IntelligenceEngine(repo).compute(req)
        forged = replace(result, quality=("FORGED_RESULT",))
        store._connection.execute("DROP TRIGGER immutable_ei_results_update")
        # A coherent forged payload still fails the independent fingerprint index.
        store._connection.execute("UPDATE ei_results SET payload=?", (dumps(forged),))
        assert validate(repo, result.intelligence_id).state.value == "INVALID"


def test_migration_from_16_preserves_old_raw_events_and_failed_ddl_is_atomic(tmp_path):
    path = tmp_path / "legacy.sqlite"
    old = migrations.MIGRATIONS[:7]
    connection = sqlite3.connect(path)
    for i, steps in enumerate(old, 1):
        for sql in steps:
            connection.execute(sql)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?)", (i, migrations.MIGRATION_VERSIONS[i - 1])
        )
    connection.commit()
    provider = source()
    legacy = batch(provider, 10, key="legacy")
    raw, event = legacy.raw, legacy.events[0]
    connection.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?)",
        (raw.observation_id, raw.source.name, raw.source_item_id, raw.ingested_at_ns, dumps(raw)),
    )
    connection.execute(
        "INSERT INTO events VALUES (?,?,?,?)",
        (event.event_id, event.revision.number, event.revision.available_from_ns, dumps(event)),
    )
    connection.execute(
        "INSERT INTO evidence VALUES (?,?,?)",
        (event.event_id, event.revision.number, raw.observation_id),
    )
    connection.commit()
    connection.close()
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.9"
        assert dumps(store.get_event(event.event_id, 10)) == dumps(event)
        assert dumps(store.get_observation(raw.observation_id)) == dumps(raw)
        assert store._rows("SELECT schema_version FROM schema_migrations ORDER BY version")[
            -2:
        ] == [("1.8",), ("1.9",)]
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        assert compute(store, b)
    broken = sqlite3.connect(":memory:")
    for i, steps in enumerate(old, 1):
        for sql in steps:
            broken.execute(sql)
        broken.execute(
            "INSERT INTO schema_migrations VALUES (?,?)", (i, migrations.MIGRATION_VERSIONS[i - 1])
        )
    broken.commit()
    broken.execute("CREATE TABLE ei_entities(conflict INTEGER)")
    broken.commit()
    with pytest.raises(PersistenceError):
        migrations.migrate(broken)
    assert (
        broken.execute("SELECT name FROM sqlite_master WHERE name='ei_policies'").fetchall() == []
    )
    assert broken.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (7,)
    broken.close()


def test_query_and_input_bounds_fail_before_partial_outputs():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        a, b = batch(p, 10, key="first"), batch(p, 20, key="second")
        ingest(store, p, a)
        ingest(store, p, b)
        repo = IntelligenceRepository(store)
        req = request(repo, b)
        with pytest.raises(ValidationError):
            IntelligenceEngine(repo, limits=IntelligenceLimits(prior_claims=1)).compute(req)
        r = compute(store, a)
        compute(store, b)
        with pytest.raises(ValidationError):
            IntelligenceQueries(
                repo, DATASET.dataset_id, limits=IntelligenceLimits(query_size=1)
            ).history(20)
        with pytest.raises(ValidationError):
            IntelligenceEngine(repo).compute(replace(req, revision_number=99))
        with pytest.raises(PersistenceError):
            IntelligenceEngine(repo).compute(replace(req, scale_assertion_id="missing"))
        assert validate(repo, r.intelligence_id).state.value != "INVALID"


def test_stored_resolution_corruption_is_detected_even_when_source_rows_are_intact():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        r = compute(store, b)
        store._connection.execute("DROP TRIGGER immutable_ce_claim_resolutions_update")
        store._connection.execute("UPDATE ce_claim_resolutions SET payload=?", (b"broken",))
        assert validate(IntelligenceRepository(store), r.intelligence_id).state.value == "INVALID"
