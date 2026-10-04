"""Additive upgrades, immutable manifests, refusal and exclusive recovery."""

import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from service_helpers import CATALOG, request, seed

from eie.core.errors import PersistenceError, SchemaCompatibilityError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.persistence import migrations
from eie.persistence.codec import loads
from eie.persistence.sqlite import SQLiteEventStore
from eie.service.compatibility import check_versions, versions
from eie.service.engine import EventIntelligenceService
from eie.service.errors import ServiceError
from eie.service.evaluation import evaluate
from eie.service.models import CoverageDeclaration, ProvenancePin
from eie.service.readiness import readiness
from eie.service.recovery import backup, restore
from eie.service.repository import ServiceRepository
from eie.service.schema import (
    CompletenessState as Q,
)
from eie.service.schema import (
    ComponentKind as C,
)
from eie.service.schema import (
    ComponentState as S,
)
from eie.service.schema import (
    ServiceCode,
)
from eie.service.validation import validate


@pytest.mark.parametrize("prior", range(13))
def test_all_prior_upgrades(tmp_path, prior):
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    for i, migration in enumerate(migrations.MIGRATIONS[:prior]):
        for statement in migration:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?)", (i + 1, migrations.MIGRATION_VERSIONS[i])
        )
    connection.commit()
    connection.close()
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "2.1"
        assert store._rows("PRAGMA foreign_key_check") == []
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "2.1"


def test_earlier_ddl_exactly_preserved():
    original = subprocess.check_output(
        ["git", "show", "HEAD:src/eie/persistence/migrations.py"], text=True
    )
    current = Path("src/eie/persistence/migrations.py").read_text()
    earlier = original[original.index("MIGRATIONS:") : original.index("def migrate(")].rstrip()
    retained = current[current.index("MIGRATIONS:") : current.index("# EIE-17")].rstrip()
    assert retained == earlier


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP INDEX service_scope",
        "DELETE FROM schema_migrations WHERE version=12",
        "INSERT INTO schema_migrations VALUES (13,'future')",
    ],
)
def test_partial_future_schema_refusal(tmp_path, mutation):
    path = tmp_path / "broken.sqlite"
    with SQLiteEventStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)


def test_upgrade_rollback(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    connection = sqlite3.connect(path)
    for i, m in enumerate(migrations.MIGRATIONS[:11]):
        for stmt in m:
            connection.execute(stmt)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?)", (i + 1, migrations.MIGRATION_VERSIONS[i])
        )
    connection.commit()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (*migrations.MIGRATIONS[:11], (*migrations.MIGRATIONS[11], "INVALID DDL")),
    )
    with pytest.raises(PersistenceError):
        migrations.migrate(connection)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (11,)
    assert not connection.execute(
        "SELECT name FROM sqlite_master WHERE name='service_publications'"
    ).fetchall()
    connection.close()


def test_immutable_and_corruption(tmp_path):
    with SQLiteEventStore(tmp_path / "store.sqlite") as store:
        engine, event = seed(store)
        repo = engine.repository
        value = engine.snapshot(request())
        with pytest.raises(PersistenceError):
            repo.append(value)
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("DELETE FROM service_snapshots")
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute("UPDATE service_publications SET fingerprint=?", ("a" * 64,))
        store._connection.execute("DROP TRIGGER immutable_service_snapshots_update")
        store._connection.execute("UPDATE service_snapshots SET fingerprint=?", ("a" * 64,))
        assert not validate(repo, value.snapshot_id).valid
        with pytest.raises(ServiceError) as exc:
            engine.snapshot(request())
        assert exc.value.code is ServiceCode.INTERNAL_VALIDATION_FAILURE


def test_owner_and_membership_corruption(tmp_path):
    with SQLiteEventStore(tmp_path / "store.sqlite") as store:
        engine, event = seed(store)
        repo = engine.repository
        value = engine.snapshot(request())
        store._connection.execute("DROP TRIGGER immutable_service_members_delete")
        store._connection.execute(
            "DELETE FROM service_members WHERE publication=?",
            (next(i for c in value.components for i in c.publication_ids),),
        )
        assert not validate(repo, value.snapshot_id).valid
    with SQLiteEventStore(tmp_path / "other.sqlite") as store:
        engine, event = seed(store)
        value = engine.snapshot(request())
        store._connection.execute("UPDATE events SET payload=? WHERE id=?", (b"{}", event.event_id))
        assert not validate(engine.repository, value.snapshot_id).valid


def test_coverage_conflicts_and_qualified_states(tmp_path):
    with SQLiteEventStore(tmp_path / "store.sqlite") as store:
        engine, _ = seed(store)
        repo = engine.repository
        for kind, state in ((C.IMPACT, Q.KNOWN_GAPS), (C.ANALOGUES, Q.NOT_APPLICABLE)):
            declaration = CoverageDeclaration(
                CATALOG,
                kind,
                request().scope,
                10,
                state,
                ProvenancePin("COVERAGE", "fixture", "a" * 64, 10),
            )
            assert repo.declare(declaration) == repo.declare(declaration)
        v = engine.snapshot(request(components=(C.ANALOGUES, C.IMPACT)))
        assert v.components[0].state is S.NOT_APPLICABLE and v.components[1].state is S.UNAVAILABLE
        # A different catalog has contradictory declarations at exactly the same time.
        for state in (Q.UNKNOWN, Q.COMPLETE):
            repo.declare(
                CoverageDeclaration(
                    "other",
                    C.IMPACT,
                    request().scope,
                    10,
                    state,
                    ProvenancePin("COVERAGE", "fixture", "a" * 64, 10),
                )
            )
        with pytest.raises(PersistenceError):
            repo.coverage(replace(request(), catalog_id="other"), C.IMPACT, (request().scope,))
        with pytest.raises(PersistenceError):
            repo.declare(
                CoverageDeclaration(
                    CATALOG,
                    C.EVENTS,
                    request().scope,
                    10,
                    Q.COMPLETE,
                    ProvenancePin("COVERAGE", "fixture", "b" * 64, 10),
                )
            )


def test_backup_restore_and_outage(tmp_path):
    db = tmp_path / "store.sqlite"
    copied = tmp_path / "copy.sqlite"
    restored = tmp_path / "restored.sqlite"
    with SQLiteEventStore(db) as store:
        engine, _ = seed(store)
        value = engine.snapshot(request())
        before = canonical_json_bytes(value)
        summary = evaluate(engine.repository, value.snapshot_id)
        assert len(summary.dimensions) == 3 and all(d.checks for d in summary.dimensions)
        manifest = backup(store, copied)
        assert manifest.store_version == "2.1" and manifest.snapshot_count == 1
        with pytest.raises(ValidationError):
            backup(store, copied)
        with store.transaction():
            with pytest.raises(PersistenceError):
                backup(store, tmp_path / "active.sqlite")
        assert readiness(engine.repository, live_ingestion=False).historical_queries
    restore(copied, restored)
    with SQLiteEventStore(restored) as store:
        assert (
            canonical_json_bytes(
                EventIntelligenceService(ServiceRepository(store)).snapshot(request())
            )
            == before
        )
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not SQLite")
    with pytest.raises(PersistenceError):
        restore(bad, tmp_path / "failed.sqlite")
    link = tmp_path / "link"
    link.symlink_to(copied)
    with pytest.raises(ValidationError):
        restore(link, tmp_path / "untrusted.sqlite")
    assert not (tmp_path / "failed.sqlite").exists()
    with SQLiteEventStore(db) as opened:
        with pytest.raises(ServiceError):
            evaluate(ServiceRepository(opened), "missing")


def test_closed_readiness(tmp_path):
    store = SQLiteEventStore(tmp_path / "store.sqlite")
    repo = ServiceRepository(store)
    store.close()
    assert not readiness(repo).historical_queries


def test_version_codec_refusal():
    with pytest.raises(SchemaCompatibilityError):
        check_versions((("engine", "future"),))
    check_versions(versions())
    with pytest.raises(PersistenceError):
        loads(b'{"__type__":"os.system","fields":{"command":"secret"}}')


def test_backup_refuses_corrupt_native_owner(tmp_path):
    with SQLiteEventStore(tmp_path / "source.sqlite") as store:
        engine, event = seed(store)
        engine.snapshot(request())
        store._connection.execute("UPDATE events SET payload=? WHERE id=?", (b"{}", event.event_id))
        with pytest.raises(PersistenceError):
            backup(store, tmp_path / "corrupt.sqlite")
        assert not (tmp_path / "corrupt.sqlite").exists()
