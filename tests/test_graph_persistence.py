"""Immutable indexed artifacts, corruption refusal, migration preservation and rollback."""

import sqlite3
from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.graph.propagation import propagate
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.graph.validation import validate_snapshot
from eie.persistence import migrations
from eie.persistence.sqlite import SQLiteEventStore
from examples.entity_relationship_graph import demo
from tests.graph_helpers import event, ingest, relationship, traversal
from tests.graph_helpers import graph as graph
from tests.test_graph_identity import listings


def test_offline_demo_restart_and_idempotence(tmp_path):
    result = demo(tmp_path)
    assert result["derived_ownership"] == 0.4 and result["restart_byte_identical"]


def test_raw_first_batch_atomic_checkpoint_idempotence_and_restart(graph):
    repo, (a, b, *_) = graph
    row = relationship("s", R.SUPPLIER_TO, a, b)
    receipt = ingest(repo, (row,), 20)
    assert ingest(repo, (row,), 20) == receipt
    assert repo.batch(receipt.batch_id) == receipt
    claim = repo.claim_revision(receipt.claim_revision_ids[0])
    assert repo.append_claim(claim) is False
    assert repo.append_node(a) is False
    assert repo.store.get_observation(receipt.observation_id).source == receipt.provider.source
    assert repo.claim_revision("unknown") is None and repo.edge_revision("unknown") is None
    snapshot = traverse(repo, (a.node_id,), 20, 10, traversal())
    assert repo.append_snapshot(snapshot) and not repo.append_snapshot(snapshot)
    assert repo.snapshot(snapshot.snapshot_id) == snapshot
    assert repo.snapshot("unknown") is None and repo.propagation("unknown") is None
    with pytest.raises(ValidationError):
        repo.append_node(replace(a, label="changed without advancing clock"))
    with pytest.raises(ValidationError):
        repo.append_claim(replace(claim, number=2, previous_revision_id=claim.revision_id))
    with pytest.raises(ValidationError):
        repo.append_claim(
            replace(
                claim,
                number=2,
                previous_revision_id=claim.revision_id,
                known_from_ns=21,
                target_node_id=a.node_id,
            )
        )
    assert validate_snapshot(snapshot, repo).valid


@pytest.mark.parametrize(
    "table",
    [
        "graph_entities",
        "graph_nodes",
        "graph_identifiers",
        "graph_edges",
        "graph_claims",
        "graph_edge_revisions",
        "graph_batches",
        "graph_snapshots",
        "graph_snapshot_edges",
        "graph_snapshot_nodes",
        "graph_listing_index",
        "graph_propagation",
    ],
)
def test_immutability_triggers(graph, table):
    repo, (a, b, *_) = graph
    listings(repo, graph[1])
    ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    repo.append_snapshot(traverse(repo, (a.node_id,), 20, 10, traversal()))
    repo.append_propagation(propagate(event(a.node_id), repo, 30, 10, traversal()))
    with pytest.raises(PersistenceError), repo.store.transaction():
        repo.store._connection.execute(f"DELETE FROM {table}")
    column = (
        "id"
        if table
        not in (
            "graph_identifiers",
            "graph_snapshot_edges",
            "graph_snapshot_nodes",
            "graph_listing_index",
        )
        else "revision"
    )
    with pytest.raises(PersistenceError), repo.store.transaction():
        repo.store._connection.execute(f"UPDATE {table} SET {column}={column}")


@pytest.mark.parametrize(
    "target",
    ["node", "claim", "canonical", "batch", "snapshot", "snapshot_nodes", "snapshot_edges"],
)
def test_corrupted_indexes_and_membership_refused(graph, target):
    repo, (a, b, *_) = graph
    receipt = ingest(repo, (relationship("s", R.SUPPLIER_TO, a, b),), 20)
    snapshot = traverse(repo, (a.node_id,), 20, 10, traversal())
    repo.append_snapshot(snapshot)
    table = {
        "node": "graph_nodes",
        "claim": "graph_claims",
        "canonical": "graph_edge_revisions",
        "batch": "graph_batches",
        "snapshot": "graph_snapshots",
        "snapshot_nodes": "graph_snapshot_nodes",
        "snapshot_edges": "graph_snapshot_edges",
    }[target]
    action = "delete" if target.startswith("snapshot_") else "update"
    with repo.store.transaction():
        repo.store._connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
        if action == "delete":
            repo.store._connection.execute(f"DELETE FROM {table}")
        else:
            column = {
                "node": "dataset",
                "claim": "provider",
                "canonical": "active",
                "batch": "fingerprint",
                "snapshot": "fingerprint",
            }[target]
            repo.store._connection.execute(
                f"UPDATE {table} SET {column}=?", ("corrupted" if target != "canonical" else 0,)
            )
    read = {
        "node": lambda: repo.node(a.node_id, 20),
        "claim": lambda: repo.current_claims(20),
        "canonical": lambda: repo.edge_revision(receipt.canonical_revision_ids[0]),
        "batch": lambda: repo.batch(receipt.batch_id),
        "snapshot": lambda: repo.snapshot(snapshot.snapshot_id),
        "snapshot_nodes": lambda: repo.snapshot(snapshot.snapshot_id),
        "snapshot_edges": lambda: repo.snapshot(snapshot.snapshot_id),
    }[target]
    with pytest.raises(PersistenceError):
        read()


def test_batch_failure_rolls_back_observation_all_edges_and_checkpoint(graph, monkeypatch):
    repo, (a, b, c, *_) = graph
    original = repo.append_claim
    calls = 0

    def fail_second(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValidationError("synthetic failure after first accepted edge")
        return original(value)

    monkeypatch.setattr(repo, "append_claim", fail_second)
    with pytest.raises(ValidationError):
        ingest(
            repo,
            (relationship("a", R.SUPPLIER_TO, a, b), relationship("b", R.SUPPLIER_TO, a, c)),
            20,
        )
    for table in ("observations", "graph_edges", "graph_claims", "graph_batches"):
        assert repo.store._rows(f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_additive_1_3_migration_retains_study_impact_and_history(tmp_path, monkeypatch):
    from examples.counterfactual_impact import demo as impact_demo

    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:4])
    with SQLiteEventStore(tmp_path / "old.sqlite") as store:
        assert store.schema_version == "1.3"
    # Existing EIE-9 artifacts are built with the same processing version for this migration check.
    impact_root = tmp_path / "impact"
    impact_root.mkdir()
    old_result = impact_demo(impact_root)
    path = impact_root / "studies.sqlite"
    connection = sqlite3.connect(path)
    old_rows = connection.execute("SELECT payload FROM impact_results ORDER BY id").fetchall()
    old_studies = connection.execute("SELECT payload FROM study_results ORDER BY id").fetchall()
    connection.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as upgraded:
        assert upgraded.schema_version == "1.4"
        assert upgraded._rows("SELECT payload FROM impact_results ORDER BY id") == old_rows
        assert upgraded._rows("SELECT payload FROM study_results ORDER BY id") == old_studies
        assert old_result["restart_byte_identical"] and not upgraded._rows(
            "PRAGMA foreign_key_check"
        )


def test_failed_graph_migration_is_transactional(tmp_path, monkeypatch):
    original = migrations.MIGRATIONS
    path = tmp_path / "old.sqlite"
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:4])
    with SQLiteEventStore(path):
        pass
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:4], (*original[4], "INVALID DDL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,), (4,)]
    assert not connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'graph_%'"
    ).fetchall()
    connection.close()
