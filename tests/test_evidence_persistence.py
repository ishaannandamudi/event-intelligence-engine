"""Atomic batches, migration, concurrent sources, normalized storage and corruption."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.engine import EvidenceEngine
from eie.evidence.models import EvidenceLimits
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, ResolutionState
from eie.evidence.validation import validate_store
from eie.ingestion.models import RetentionPermission
from eie.persistence import migrations
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput
from examples.evidence_demo import demonstrate
from tests.evidence_helpers import CLAIM, DATASET, TIME, bare, item, setup, source

TABLES = (
    "ce_datasets",
    "ce_acquisitions",
    "ce_claims",
    "ce_references",
    "ce_assertions",
    "ce_assertion_evidence",
    "ce_relations",
    "ce_descriptors",
    "ce_blocks",
    "ce_claim_resolutions",
    "ce_resolution_inputs",
    "ce_resolution_relations",
    "ce_candidates",
    "ce_event_runs",
    "ce_run_inputs",
    "ce_run_candidates",
    "ce_group_members",
    "ce_receipts",
    "ce_seals",
)


def populated(store):
    repo, engine, queries = setup(store)
    a, b = source("ALPHA"), source("BETA")
    first = item(a, 100)
    engine.ingest(a, first)
    receipt = engine.ingest(b, item(b, 200))
    engine.freeze(queries, 200)
    return repo, engine, queries, receipt


def test_example_restart_and_deterministic_fingerprints(tmp_path):
    a = demonstrate(tmp_path / "first.sqlite")
    b = demonstrate(tmp_path / "second.sqlite")
    assert a == b and a["restart_byte_identical"]


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("action", ("UPDATE", "DELETE"))
def test_all_artifact_immutability_triggers(table, action):
    with SQLiteEventStore(":memory:") as store:
        populated(store)
        column = (
            "id"
            if table
            not in (
                "ce_assertion_evidence",
                "ce_blocks",
                "ce_resolution_inputs",
                "ce_resolution_relations",
                "ce_run_inputs",
                "ce_run_candidates",
                "ce_group_members",
                "ce_seals",
            )
            else {
                "ce_assertion_evidence": "assertion",
                "ce_blocks": "descriptor",
                "ce_resolution_inputs": "resolution",
                "ce_resolution_relations": "resolution",
                "ce_run_inputs": "run",
                "ce_run_candidates": "run",
                "ce_group_members": "run",
                "ce_seals": "known",
            }[table]
        )
        assert store._rows(f"SELECT COUNT(*) FROM {table}")[0][0] > 0
        with pytest.raises(PersistenceError), store.transaction():
            store._connection.execute(
                f"DELETE FROM {table}"
                if action == "DELETE"
                else f"UPDATE {table} SET {column}={column}"
            )


@pytest.mark.parametrize(
    "target",
    (
        "dataset",
        "acquisition",
        "claim",
        "reference",
        "assertion",
        "assertion_links",
        "relationship",
        "descriptor",
        "blocks",
        "resolution",
        "resolution_inputs",
        "resolution_relations",
        "candidate",
        "run",
        "run_inputs",
        "run_candidates",
        "group",
        "receipt",
    ),
)
def test_corrupt_indices_and_missing_memberships_fail(target):
    with SQLiteEventStore(":memory:") as store:
        repo, _, _, receipt = populated(store)
        tables = {
            "dataset": ("ce_datasets", "fingerprint"),
            "acquisition": ("ce_acquisitions", "known"),
            "claim": ("ce_claims", "predicate"),
            "reference": ("ce_references", "content_hash"),
            "assertion": ("ce_assertions", "provider"),
            "assertion_links": ("ce_assertion_evidence", None),
            "relationship": ("ce_relations", "known"),
            "descriptor": ("ce_descriptors", "known"),
            "blocks": ("ce_blocks", None),
            "resolution": ("ce_claim_resolutions", "state"),
            "resolution_inputs": ("ce_resolution_inputs", None),
            "resolution_relations": ("ce_resolution_relations", None),
            "candidate": ("ce_candidates", "decision"),
            "run": ("ce_event_runs", "policy"),
            "run_inputs": ("ce_run_inputs", None),
            "run_candidates": ("ce_run_candidates", None),
            "group": ("ce_group_members", None),
            "receipt": ("ce_receipts", "generation"),
        }
        table, column = tables[target]
        action = "delete" if column is None else "update"
        with store.transaction():
            store._connection.execute(f"DROP TRIGGER immutable_{table}_{action}")
            if column is None:
                store._connection.execute(f"DELETE FROM {table}")
            else:
                store._connection.execute(
                    f"UPDATE {table} SET {column}=?",
                    (999 if column in ("known", "generation") else "corrupt",),
                )
        with pytest.raises((PersistenceError, ValidationError)):
            repo.receipt(receipt.receipt_id)
            validate_store(repo)


@pytest.mark.parametrize("failure", ("reference", "claim", "descriptor", "receipt"))
def test_atomic_failure_leaves_no_partial_item(failure, monkeypatch):
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        method = {
            "reference": "append_reference",
            "claim": "append_assertion",
            "descriptor": "append_descriptor",
            "receipt": "append_receipt",
        }[failure]
        original = getattr(repo, method)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValidationError("deliberate late item failure")

        monkeypatch.setattr(repo, method, fail)
        p = source("ALPHA")
        batch = item(p, 100)
        with pytest.raises(ValidationError):
            engine.ingest(p, batch)
        assert store.get_observation(batch.raw.observation_id) is None
        assert all(store._rows(f"SELECT COUNT(*) FROM {t}")[0][0] == 0 for t in TABLES)


def test_two_connections_conflicting_provider_race(tmp_path):
    path = tmp_path / "race.sqlite"
    with SQLiteEventStore(path) as first, SQLiteEventStore(path) as second:
        barrier = Barrier(2)
        providers = (source("ALPHA"), source("BETA"))
        values = (TIME, replace(TIME, timestamp_ns=TIME.timestamp_ns + 1))

        def ingest(i):
            barrier.wait(timeout=10)
            repo = EvidenceRepository((first, second)[i])
            return EvidenceEngine(repo).ingest(providers[i], item(providers[i], 100, values[i]))

        with ThreadPoolExecutor(max_workers=2) as workers:
            receipts = tuple(workers.map(ingest, (0, 1)))
        repo = EvidenceRepository(first)
        queries = EvidenceQueries(repo, DATASET.dataset_id)
        result = queries.resolution(CLAIM.claim_id, 100)
        assert result.state is ResolutionState.UNRESOLVED
        assert len(result.alternative_ids) == 2 and len(result.evidence_ids) == 2
        assert {repo.receipt(r.receipt_id) for r in receipts} == set(receipts)
        frozen = dumps(result)
    with SQLiteEventStore(path) as store:
        repo = EvidenceRepository(store)
        assert (
            dumps(EvidenceQueries(repo, DATASET.dataset_id).resolution(CLAIM.claim_id, 100))
            == frozen
        )
        validate_store(repo)


def test_freeze_and_validation_hold_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "lock.sqlite"
    with SQLiteEventStore(path) as first, SQLiteEventStore(path) as second:
        repo, engine, queries = setup(first)
        p = source("ALPHA")
        engine.ingest(p, item(p, 100))
        started, blocked = Event(), Event()
        second._connection.execute("PRAGMA busy_timeout=0")
        original = repo.append_resolution

        def guarded(value, inputs, links):
            started.set()
            with pytest.raises(PersistenceError):
                EvidenceEngine(EvidenceRepository(second)).ingest(
                    source("BETA"), item(source("BETA"), 100)
                )
            blocked.set()
            return original(value, inputs, links)

        monkeypatch.setattr(repo, "append_resolution", guarded)
        engine.freeze(queries, 100)
        assert started.is_set() and blocked.is_set()
        with pytest.raises(ValidationError):
            EvidenceEngine(EvidenceRepository(second)).ingest(
                source("BETA"), item(source("BETA"), 100)
            )
        assert second._rows("SELECT COUNT(*) FROM ce_assertions")[0][0] == 1


def test_normalized_evidence_reuse_and_multi_event_bundle():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        p = source("ALPHA")
        batch = item(p, 100)
        from eie.events.identity import EventIdentity
        from eie.events.taxonomy import EventFamily, EventType
        from eie.evidence.claims import Claim, OccurrenceValue
        from eie.evidence.schema import ClaimType, Predicate

        event = replace(
            batch.events[0],
            identity=EventIdentity(
                EventType(EventFamily.MANAGEMENT_GOVERNANCE, "MANAGEMENT.CEO_DEPARTURE"),
                batch.events[0].identity.scope,
                "departure",
            ),
        )
        descriptor = replace(
            batch.descriptors[0],
            event_id=event.event_id,
            event_type=event.identity.event_type,
            corporate_identity=None,
            provider_event_key="departure",
            period=None,
        )
        occurrence = Claim(ClaimType.EVENT_OCCURRENCE, CLAIM.subject, Predicate.OCCURRED)
        many = (
            ClaimInput(
                "occurred",
                occurrence,
                OccurrenceValue(True),
                (batch.references[0].evidence_id,),
                event_id=event.event_id,
            ),
            *batch.claims,
        )
        combined = replace(
            batch,
            claims=many,
            events=tuple(sorted((*batch.events, event), key=lambda e: e.revision_id)),
            descriptors=tuple(
                sorted((*batch.descriptors, descriptor), key=lambda d: d.descriptor_id)
            ),
        )
        receipt = engine.ingest(p, combined)
        assert store._rows("SELECT COUNT(*) FROM ce_references")[0][0] == 1
        assert store._rows("SELECT COUNT(*) FROM ce_assertion_evidence")[0][0] == 2
        assert len(queries.event_resolution(100).groups) == 2
        assert len(queries.source_item(batch.raw.observation_id, 100)) == 2
        assert len(queries.traverse(batch.raw.observation_id, 100).claim_ids) == 2
        assert queries.traverse(batch.raw.observation_id, 100, max_results=1).truncated
        assert repo.receipt(receipt.receipt_id) == receipt


def test_additive_migration_from_14_preserves_original_bytes(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:5])
    with SQLiteEventStore(path) as store:
        raw = item(source("ALPHA"), 100).raw
        store.append_observation(raw)
        before = store._rows("SELECT payload FROM observations")[0][0]
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.8"
        assert store._rows("SELECT payload FROM observations")[0][0] == before
        assert store.get_observation(raw.observation_id) == raw
        setup(store)[1].ingest(source("BETA"), item(source("BETA"), 200))


def test_failed_migration_rolls_back_all_new_ddl(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:5])
    with SQLiteEventStore(path):
        pass
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:5], (*original[5], "INVALID DDL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5
        assert (
            conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ce_%'").fetchone()[0]
            == 0
        )


def test_invalid_batches_retention_bounds_missing_refs_and_seals():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        p = source("ALPHA")
        batch = item(p, 100)
        for provider, changed in (
            (replace(p, normalized_retention=RetentionPermission.RESTRICTED), batch),
            (replace(p, parser_version="2"), batch),
            (replace(p, capabilities=frozenset({ClaimType.ACTUAL_VALUE})), batch),
            (p, replace(batch, references=())),
            (p, replace(batch, references=(replace(batch.references[0], content_hash="b" * 64),))),
        ):
            with pytest.raises(ValidationError):
                engine.ingest(provider, changed)
        assert store._rows("SELECT COUNT(*) FROM observations")[0][0] == 0
        with pytest.raises(ValidationError):
            engine.ingest(p, batch, source_payload=b"wrong")
        with pytest.raises(PersistenceError):
            repo.append_claim(CLAIM)
        engine.ingest(p, batch)
        engine.freeze(queries, 100)
        assert engine.ingest(p, batch).generation == 1
        with pytest.raises(ValidationError):
            engine.ingest(source("BETA"), item(source("BETA"), 99))
        for method in (
            repo.reference,
            repo.assertion,
            repo.lineage,
            repo.dataset,
            repo.relationship,
            repo.descriptor,
            repo.stored_resolution,
            repo.candidate,
            repo.stored_event_run,
        ):
            with pytest.raises(PersistenceError):
                method("missing")
        assert repo.receipt("missing") is None
        with pytest.raises(ValidationError):
            queries.traverse(batch.raw.observation_id, 100, depth=9)
        with pytest.raises(ValidationError):
            queries.traverse(batch.raw.observation_id, 100, max_results=0)
        with pytest.raises(ValidationError):
            engine.freeze(EvidenceQueries(EvidenceRepository(store), DATASET.dataset_id), 100)


def test_batch_claim_limit_failure_is_atomic():
    with SQLiteEventStore(":memory:") as store:
        repo, _, _ = setup(store)
        p = source("ALPHA")
        batch = bare(p)
        two = replace(
            batch, claims=(batch.claims[0], replace(batch.claims[0], provider_claim_key="z"))
        )
        with pytest.raises(ValidationError):
            EvidenceEngine(repo, limits=EvidenceLimits(claims_per_item=1)).ingest(p, two)
        assert store._rows("SELECT COUNT(*) FROM observations")[0][0] == 0
