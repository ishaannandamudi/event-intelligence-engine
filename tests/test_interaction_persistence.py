import os
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.interactions.engine import InteractionEngine
from eie.interactions.repository import InteractionRepository
from eie.interactions.validation import validate
from eie.persistence import migrations
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.interaction_fixtures import DATASET, MINUTE, query, retained_event
from tests.test_interaction_collisions import repo as repo


def test_restart_and_hashseeds_preserve_exact_bytes(tmp_path):
    path = tmp_path / "saved.sqlite"
    with SQLiteEventStore(path) as store:
        repo = InteractionRepository(store)
        a = retained_event(repo, "a", MINUTE, time=MINUTE)
        retained_event(repo, "b", MINUTE, time=MINUTE)
        context = InteractionEngine(repo).interaction_context(query(repo, a, MINUTE))
        before = dumps(context)
        validate(repo, context.artifact_id, deep=True)
    with SQLiteEventStore(path) as store:
        assert dumps(InteractionRepository(store).context(context.artifact_id)) == before
    program = (
        "from eie.persistence.sqlite import SQLiteEventStore; "
        "from eie.interactions.repository import InteractionRepository; import sys; "
        "s=SQLiteEventStore(sys.argv[1]); "
        "print(InteractionRepository(s).context(sys.argv[2]).fingerprint); s.close()"
    )
    for seed in ("1", "31", "random"):
        result = subprocess.run(
            [sys.executable, "-c", program, str(path), context.artifact_id],
            env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == context.fingerprint


def test_insertion_order_is_deterministic(tmp_path):
    values = []
    for reverse in (False, True):
        with SQLiteEventStore(tmp_path / f"{reverse}.sqlite") as store:
            repo = InteractionRepository(store)
            events = (
                {k: retained_event(repo, k, MINUTE, time=MINUTE) for k in ("b", "a") if reverse}
                if reverse
                else {k: retained_event(repo, k, MINUTE, time=MINUTE) for k in ("a", "b")}
            )
            values.append(
                dumps(InteractionEngine(repo).interaction_context(query(repo, events["a"], MINUTE)))
            )
    assert values[0] == values[1]


def test_snapshot_failure_rolls_back_every_child_and_seal(repo, monkeypatch):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(repo, "b", MINUTE, time=MINUTE)
    q = query(repo, a, MINUTE)
    before = repo.store._rows("SELECT COUNT(*) FROM interaction_artifacts")[0][0]

    def broken(*args):
        raise ValidationError("synthetic persistence failure")

    monkeypatch.setattr(repo, "save_context", broken)
    with pytest.raises(ValidationError):
        InteractionEngine(repo).interaction_context(q)
    assert repo.store._rows("SELECT COUNT(*) FROM interaction_artifacts") == [(before,)]
    assert repo.store._rows("SELECT COUNT(*) FROM interaction_contexts") == [(0,)]
    assert repo.store._rows("SELECT COUNT(*) FROM interaction_pairs") == [(0,)]
    assert repo.store._rows("SELECT COUNT(*) FROM interaction_seals") == [(0,)]


def test_concurrent_future_insert_uses_single_sqlite_snapshot(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from eie.interactions import engine

    with SQLiteEventStore(tmp_path / "concurrent.sqlite") as store:
        repo = InteractionRepository(store)
        a = retained_event(repo, "a", MINUTE, time=MINUTE)
        q = query(repo, a, MINUTE)
        with SQLiteEventStore(tmp_path / "concurrent.sqlite") as writer:
            started, finished = Event(), Event()
            original = engine.collect

            def append():
                started.set()
                retained_event(InteractionRepository(writer), "future", 2 * MINUTE, time=2 * MINUTE)
                finished.set()

            with ThreadPoolExecutor(max_workers=1) as workers:
                pending = None

                def hooked(*args):
                    nonlocal pending
                    pending = workers.submit(append)
                    assert started.wait(5) and not finished.is_set()
                    return original(*args)

                monkeypatch.setattr(engine, "collect", hooked)
                context = InteractionEngine(repo).interaction_context(q)
                pending.result(timeout=10)
                assert finished.is_set() and not context.collision_ids
                validate(repo, context.artifact_id, deep=True)


def test_sealing_is_append_only_and_reindex_is_idempotent(repo):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    q = query(repo, a, 2 * MINUTE)
    context = InteractionEngine(repo).interaction_context(q)
    assert repo.index_event(DATASET, a.event_id, MINUTE).artifact_id == context.target_row_id
    with pytest.raises(ValidationError, match="sealed"):
        retained_event(repo, "backdated", MINUTE, time=MINUTE)
    with pytest.raises(PersistenceError):
        repo.event("missing")
    with pytest.raises(ValidationError):
        repo.index_event(DATASET, "missing", MINUTE)
    with pytest.raises(PersistenceError):
        repo.context("missing")
    with pytest.raises(PersistenceError):
        repo.manifest("missing")
    with pytest.raises(PersistenceError):
        validate(repo, "missing")


@pytest.mark.parametrize(
    "table,column",
    [
        ("interaction_events", "fingerprint"),
        ("interaction_artifacts", "fingerprint"),
        ("interaction_contexts", "fingerprint"),
    ],
)
def test_corruption_fails_closed(repo, table, column):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    q = query(repo, a, MINUTE)
    context = InteractionEngine(repo).interaction_context(q)
    repo.store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
    repo.store._connection.execute(f"UPDATE {table} SET {column}='broken'")
    with pytest.raises(PersistenceError):
        repo.context(context.artifact_id)


@pytest.mark.parametrize(
    "table",
    ["interaction_entities", "interaction_bundles", "interaction_members", "interaction_pairs"],
)
def test_membership_corruption_detected(repo, table):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(repo, "b", MINUTE, time=MINUTE)
    context = InteractionEngine(repo).interaction_context(query(repo, a, MINUTE))
    repo.store._connection.execute(f"DROP TRIGGER immutable_{table}_delete")
    repo.store._connection.execute(f"DELETE FROM {table}")
    with pytest.raises(PersistenceError):
        repo.context(context.artifact_id)


@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_immutability_triggers_for_all_new_tables(repo, action):
    for table in (
        "interaction_events",
        "interaction_entities",
        "interaction_bundles",
        "interaction_artifacts",
        "interaction_contexts",
        "interaction_members",
        "interaction_pairs",
        "interaction_chronology",
        "interaction_seals",
    ):
        triggers = repo.store._rows(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (f"immutable_{table}_{action.lower()}",),
        )
        assert triggers
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    context = InteractionEngine(repo).interaction_context(query(repo, a, MINUTE))
    with pytest.raises(sqlite3.IntegrityError):
        repo.store._connection.execute(
            "UPDATE interaction_contexts SET known=0"
            if action == "UPDATE"
            else "DELETE FROM interaction_contexts"
        )
    assert repo.context(context.artifact_id) == context


def test_additive_migration_18_to_19_and_transactional_rollback(tmp_path, monkeypatch):
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:9])
    path = tmp_path / "legacy.sqlite"
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.8"
        store._connection.execute("INSERT INTO sessions VALUES ('old','1.0',?)", (b"unchanged",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:9], (*original[9], "invalid SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT schema_version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone() == ("1.8",)
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='interaction_events'"
    ).fetchall()
    conn.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "2.1" and store._rows(
            "SELECT payload FROM sessions WHERE namespace='old'"
        ) == [(b"unchanged",)]


def test_run_is_bounded_atomic_and_persisted(repo):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    b = retained_event(repo, "b", MINUTE, time=MINUTE)
    q = query(repo, a, MINUTE)
    engine = InteractionEngine(repo)
    run = engine.run((q, replace(q, target_event_id=b.event_id)))
    from eie.interactions.models import InteractionRunManifest

    assert len(repo.artifact(run, InteractionRunManifest).context_ids) == 2
    with pytest.raises(ValidationError):
        engine.run(())
    with pytest.raises(ValidationError):
        engine.run((q, q))
    with pytest.raises(ValidationError):
        engine.run((q, replace(q, known_as_of_ns=2 * MINUTE)))
