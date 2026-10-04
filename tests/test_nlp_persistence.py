"""Atomic ingestion, exact restart, source changes, entitlement and indexed queries."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from nlp_helpers import BODY, CATALOG, DATASET, ISSUER, item, model, source

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.queries import EvidenceQueries
from eie.evidence.schema import ClaimStatus, ClaimType
from eie.ingestion.models import RetentionPermission
from eie.nlp.engine import TextEngine
from eie.nlp.models import ExtractionSettings
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.schema import RunStatus, SourceAction, UnscheduledFamily
from eie.nlp.validation import validate_store
from eie.nlp.validation_models import ValidationStatus
from eie.persistence import migrations
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.text_demo import demonstrate


def setup(store):
    repo = TextRepository(store)
    return repo, TextEngine(repo), TextQueries(repo, DATASET.dataset_id)


def test_demo_restart_and_recalculation(tmp_path):
    result = demonstrate(tmp_path / "text.sqlite")
    assert result["restart_byte_identical"] and result["eie11_conflict_preserved"]
    with SQLiteEventStore(tmp_path / "text.sqlite") as store:
        repo, _, q = setup(store)
        assert validate_store(repo).status is ValidationStatus.VALID
        assert len(q.documents(400)) == 2
        assert len(q.failed_extractions(400)) == 1
        assert len(q.event_candidates(400, family=UnscheduledFamily.BUYBACK, entity_id=ISSUER)) == 3
        assert q.accepted_claims(400)
        run = q.extractions_as_of(200)[0]
        assert q.document(run.document_id, 200).document_id == run.document_id
        assert q.mentions(run.run_id, 200)
        assert q.event_audits(run.accepted[0].event_id, 200)
        assert not q.documents(199)
        with pytest.raises(ValidationError):
            q.document(run.document_id, 199)
        with pytest.raises(ValidationError):
            q.extraction_run(run.run_id, 199)
        other = TextQueries(repo, "eds_" + "b" * 64)
        with pytest.raises(ValidationError):
            other.extraction_run(run.run_id, 400)
        assert q.extractions_as_of(400, status=RunStatus.PARTIAL)


def test_correction_and_model_reprocessing_cannot_mutate_prior_views(tmp_path):
    with SQLiteEventStore(tmp_path / "history.sqlite") as store:
        repo, engine, q = setup(store)
        p = source()
        doc, b = item(p)
        run = engine.ingest(p, doc, b, CATALOG, 200, model=model())
        before = dumps(q.audit(run.run_id, 200))
        assert engine.ingest(p, doc, b, CATALOG, 200, model=model()) == run
        newp = replace(p, settings=ExtractionSettings(parser_version="financial-text-2"))
        second = engine.ingest(
            newp, doc, b, CATALOG, 300, model=model(version="2"), previous_run_id=run.run_id
        )
        assert run.run_id != second.run_id and second.previous_run_id == run.run_id
        corrected, body = item(
            p,
            BODY.replace("$5.2 billion", "$5.3 billion"),
            350,
            version="2",
            action=SourceAction.CORRECTED,
            previous=doc,
        )
        correction = engine.ingest(p, corrected, body, CATALOG, 400, previous_run_id=second.run_id)
        assertions = [
            repo.evidence.assertion(i) for a in correction.accepted for i in a.assertion_ids
        ]
        assert all(a.status is ClaimStatus.CORRECTED for a in assertions)
        assert any(
            repo.evidence.claim(a.claim_id).claim_type is ClaimType.ACTUAL_VALUE for a in assertions
        )
        assert dumps(q.audit(run.run_id, 200)) == before
        assert validate_store(repo).status is ValidationStatus.VALID
        assert repo.content(doc.document_id) == b and repo.content(corrected.document_id) == body
        assert len(q.extractions_as_of(200)) == 1 and len(q.extractions_as_of(400)) == 3


def test_source_retraction_reaches_eie11_and_retains_old_artifacts(tmp_path):
    with SQLiteEventStore(tmp_path / "withdrawal.sqlite") as store:
        repo, engine, q = setup(store)
        p = source()
        doc, b = item(p)
        run = engine.ingest(p, doc, b, CATALOG, 200)
        before = dumps(q.audit(run.run_id, 200))
        withdrawn, payload = item(
            p,
            "The provider retracted this source article.",
            300,
            version="2",
            action=SourceAction.RETRACTED,
            previous=doc,
        )
        retraction = engine.ingest(p, withdrawn, payload, CATALOG, 400)
        assert retraction.retracted_assertion_ids and not retraction.accepted
        assert all(
            repo.evidence.assertion(i).status is ClaimStatus.RETRACTED
            for i in retraction.retracted_assertion_ids
        )
        assert engine.ingest(p, withdrawn, payload, CATALOG, 400) == retraction
        claim = run.accepted[0].claim_ids[0]
        assert (
            EvidenceQueries(repo.evidence, DATASET.dataset_id).resolution(claim, 400).value is None
        )
        assert dumps(q.audit(run.run_id, 200)) == before
        assert validate_store(repo).status is ValidationStatus.VALID
        assert q.audit(retraction.run_id, 400).assertions


def test_metadata_only_does_not_gain_content_from_another_provider(tmp_path):
    with SQLiteEventStore(tmp_path / "retention.sqlite") as store:
        repo, engine, q = setup(store)
        a, b = source(), source("MIRROR", retain=False, family="OFFICIAL")
        doc, body = item(a)
        other, content = item(b, observed=300)
        engine.ingest(a, doc, body, CATALOG, 200)
        engine.ingest(b, other, content, CATALOG, 400)
        assert repo.content(doc.document_id) == body and repo.content(other.document_id) is None
        assert store._rows("SELECT COUNT(*) FROM te_contents") == [(1,)]
        assert len(q.documents(400)) == 2
        assert validate_store(repo).status is ValidationStatus.VALID_WITH_WARNINGS
        assert store._rows("SELECT COUNT(*) FROM observations") == [(2,)]


def test_atomic_rollback_after_evidence_and_after_text_data(tmp_path, monkeypatch):
    with SQLiteEventStore(tmp_path / "atomic.sqlite") as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        original = repo.append_run

        def fail(run):
            raise PersistenceError("injected persistence failure")

        monkeypatch.setattr(repo, "append_run", fail)
        with pytest.raises(PersistenceError):
            engine.ingest(p, doc, b, CATALOG, 200)
        for table in (
            "observations",
            "events",
            "ce_datasets",
            "ce_receipts",
            "te_documents",
            "te_artifacts",
            "te_runs",
        ):
            assert store._rows(f"SELECT COUNT(*) FROM {table}") == [(0,)]
        monkeypatch.setattr(repo, "append_run", original)
        assert engine.ingest(p, doc, b, CATALOG, 200).accepted


def test_no_unverified_candidate_enters_events_or_claims(tmp_path):
    with SQLiteEventStore(tmp_path / "reject.sqlite") as store:
        repo, engine, q = setup(store)
        p = source()
        doc, b = item(p, "CIK 0000320193 CEO did not resign")
        run = engine.ingest(p, doc, b, CATALOG, 200)
        assert run.status is RunStatus.REJECTED and not run.accepted
        assert store._rows("SELECT COUNT(*) FROM events") == [(0,)]
        assert store._rows("SELECT COUNT(*) FROM ce_assertions") == [(0,)]
        assert q.failed_extractions(200) == (run,)
        assert validate_store(repo).status is ValidationStatus.VALID
        absent, payload = item(p, "No recognizable source event.", 300, item_id="empty")
        empty = engine.ingest(p, absent, payload, CATALOG, 400)
        assert empty.status is RunStatus.FAILED
        assert validate_store(repo).status is ValidationStatus.VALID


@pytest.mark.parametrize(
    "change",
    ["source", "family", "dataset", "retention", "timezone", "currency", "parser", "entitlement"],
)
def test_source_configuration_is_not_text_controlled(tmp_path, change):
    p = source()
    doc, b = item(p)
    if change == "source":
        doc = replace(
            doc,
            raw=replace(doc.raw, source=source("DIFFERENT").evidence.source),
            source_lineage=replace(
                doc.source_lineage,
                raw_observation_id=replace(
                    doc.raw, source=source("DIFFERENT").evidence.source
                ).observation_id,
            ),
        )
    if change == "family":
        doc = replace(doc, source_family="FORGED")
    if change == "dataset":
        doc = replace(doc, dataset_id="eds_" + "b" * 64)
    if change == "retention":
        doc = replace(doc, retention=source(retain=False).evidence.archive_policy)
    if change == "timezone":
        doc = replace(doc, timezone="UTC")
    if change == "currency":
        doc = replace(doc, currency="EUR")
    if change == "parser":
        doc = replace(doc, source_lineage=replace(doc.source_lineage, parser_version="forged"))
    if change == "entitlement":
        p = replace(
            p, evidence=replace(p.evidence, normalized_retention=RetentionPermission.UNKNOWN)
        )
    with SQLiteEventStore(tmp_path / (change + ".sqlite")) as store:
        with pytest.raises(ValidationError):
            TextEngine(TextRepository(store)).ingest(p, doc, b, CATALOG, 200)
        assert store._rows("SELECT COUNT(*) FROM te_runs") == [(0,)]


def test_knowledge_monotonic_and_failed_writes_leave_no_partial_source(tmp_path):
    with SQLiteEventStore(tmp_path / "causal.sqlite") as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        run = engine.ingest(p, doc, b, CATALOG, 200)
        d, payload = item(source("B"), observed=150)
        with pytest.raises(ValidationError):
            engine.ingest(source("B"), d, payload, CATALOG, 199)
        with pytest.raises(ValidationError):
            engine.ingest(p, doc, b, CATALOG, 200, model=model(version="new"))
        with pytest.raises(PersistenceError):
            engine.ingest(p, doc, b, CATALOG, 300, previous_run_id="trun_" + "a" * 64)
        assert repo.run(run.run_id) == run


def test_concurrent_same_source_is_idempotent(tmp_path):
    with SQLiteEventStore(tmp_path / "threads.sqlite") as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: engine.ingest(p, doc, b, CATALOG, 200), range(4)))
        assert len({r.result_fingerprint for r in runs}) == 1
        assert store._rows("SELECT COUNT(*) FROM te_runs") == [(1,)]
        assert validate_store(repo).status is ValidationStatus.VALID


def test_additive_15_migration_preserves_raw_and_prior_ddl(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    all_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations[:6])
    p = source()
    doc, _ = item(p)
    with SQLiteEventStore(path) as store:
        store.append_observation(doc.raw)
        before = store._rows("SELECT payload FROM observations")
    monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations)
    with SQLiteEventStore(path) as store:
        assert (
            store.schema_version == "2.1"
            and store._rows("SELECT payload FROM observations") == before
        )
        assert validate_store(TextRepository(store)).status is ValidationStatus.VALID


def test_completed_rows_are_immutable(tmp_path):
    with SQLiteEventStore(tmp_path / "immutable.sqlite") as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        engine.ingest(p, doc, b, CATALOG, 200, model=model())
        for table in (
            "te_contents",
            "te_documents",
            "te_maps",
            "te_segments",
            "te_catalogs",
            "te_templates",
            "te_artifacts",
            "te_entity_members",
            "te_runs",
            "te_members",
            "te_run_segments",
            "te_accepted",
            "te_span_evidence",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                store._connection.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.IntegrityError):
                store._connection.execute(f"UPDATE {table} SET rowid=rowid")


@pytest.mark.parametrize(
    "table,column,expression",
    [
        ("te_documents", "observed", "observed+1"),
        ("te_maps", "normalized_hash", "'" + "b" * 64 + "'"),
        ("te_segments", "start", "start+1"),
        ("te_catalogs", "id", "'wrong'"),
        ("te_templates", "version", "'wrong'"),
        ("te_artifacts", "family", "'WRONG'"),
        ("te_runs", "fingerprint", "'wrong'"),
        ("te_contents", "content", "x'01'"),
    ],
)
def test_corrupt_indexes_or_content_are_invalid(tmp_path, table, column, expression):
    with SQLiteEventStore(tmp_path / (table + ".sqlite")) as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        engine.ingest(p, doc, b, CATALOG, 200, model=model())
        store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
        store._connection.execute("PRAGMA foreign_keys=OFF")
        store._connection.execute(f"UPDATE {table} SET {column}={expression}")
        assert validate_store(repo).status is ValidationStatus.INVALID


def test_bounded_queries_fail_instead_of_silent_truncation(tmp_path):
    with SQLiteEventStore(tmp_path / "bounds.sqlite") as store:
        repo, engine, _ = setup(store)
        p = source()
        doc, b = item(p)
        run = engine.ingest(p, doc, b, CATALOG, 200)
        small = TextQueries(repo, DATASET.dataset_id, limit=1)
        with pytest.raises(ValidationError):
            small.event_candidates(200)
        with pytest.raises(ValidationError):
            small.mentions(run.run_id, 200)
        with pytest.raises(ValidationError):
            small.accepted_claims(200)
        with pytest.raises(ValidationError):
            TextQueries(repo, DATASET.dataset_id, limit=0)
