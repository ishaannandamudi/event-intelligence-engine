"""Frozen lineage, validation, explicit version policy and compatible partition reuse."""

import sqlite3
from dataclasses import replace

import pytest
from historical_helpers import fixture, item, one, page, workspace

from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import GapReason, JobStatus, ProvenanceKind, TimeRange
from eie.backfill.rebuild import rebuild
from eie.backfill.reuse import reuse_completed
from eie.backfill.sources import ContractHistoryProcessor, FixtureHistoricalSource
from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.builder import finalize
from eie.datasets.models import BackfillJob, ReprocessPolicy, ValidationStatus
from eie.datasets.repository import typed
from eie.datasets.validation import validate_dataset
from eie.ingestion.models import RetentionPolicy
from eie.persistence import codec
from eie.runtime.clock import ManualClock


def test_manifest_lineage_versions_export_and_rebuild_immutability(tmp_path):
    job, source, processor = fixture()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        manifest = repo.manifest(job.build_id)
        original = codec.dumps(manifest)
        assert codec.loads(original) == manifest
        assert dict(manifest.record_counts)["CanonicalEvents"] == 2
        assert dict(manifest.record_counts)["RawSourceObservations"] == 2
        assert (
            manifest.job.definition.engine_version == "1.0.0"
            and manifest.store_schema_version == "1.3"
        )
        assert len(repo.lineage(job.build_id)) == 2
        for lineage in repo.lineage(job.build_id):
            assert lineage.adapter_version == processor.adapter_version
            assert lineage.parser_version == processor.parser_version
            assert lineage.settings_fingerprint == job.definition.settings.fingerprint
            assert lineage.object_hash in manifest.archive_hashes
        replay = rebuild(
            repo, archive, job.build_id, {"SYNTH_HIST": processor}, "same", clock=ManualClock(2000)
        )
        assert repo.manifest(replay.build_id).content_fingerprint == manifest.content_fingerprint
        assert all(
            a.provenance is ProvenanceKind.ARCHIVE_REPLAYED
            and a.replayed_at_ns == 2000
            and a.acquired_at_ns == 1000
            for a in repo.acquisitions(replay.build_id)
        )
        assert codec.dumps(repo.manifest(job.build_id)) == original
        assert source.requests == 2
        assert finalize(repo, job, 3000) == manifest
        for table in (
            "historical_jobs",
            "historical_partitions",
            "historical_acquisitions",
            "historical_observations",
            "historical_facts",
            "historical_lineage",
            "dataset_manifests",
        ):
            column = "progress" if table == "historical_partitions" else "payload"
            with pytest.raises(sqlite3.IntegrityError):
                repo.store._connection.execute(f"UPDATE {table} SET {column}={column}")
        with pytest.raises(ValidationError):
            replace(manifest, status=JobStatus.RUNNING)
        with pytest.raises(ValidationError):
            replace(manifest, records_fingerprint="bad")
        with pytest.raises(ValidationError):
            replace(manifest, record_counts=(("x", -1),))
        with pytest.raises(PersistenceError):
            typed(codec.dumps(()), BackfillJob)


def test_deterministic_content_excludes_acquisition_and_definition_creation_clocks(tmp_path):
    first, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(first)
        original = repo.manifest(first.build_id)
        second = BackfillJob(replace(first.definition, created_at_ns=8000), "second")
        runtime.clock = ManualClock(10000)
        runtime.run(second)
        assert repo.manifest(second.build_id).content_fingerprint == original.content_fingerprint
        assert repo.facts(first.build_id)[0].raw.source_observed_at_ns == 1000
        assert repo.facts(second.build_id)[0].raw.source_observed_at_ns == 10000


@pytest.mark.parametrize(
    "policy", [ReprocessPolicy.NO_REPROCESS, ReprocessPolicy.REPROCESS_WITH_NEW_VERSION]
)
def test_rebuild_policy_cannot_silently_change_interpretation(tmp_path, policy):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        with pytest.raises(ValidationError):
            rebuild(
                repo, archive, job.build_id, {"SYNTH_HIST": processor}, "reprocess", policy=policy
            )
        with pytest.raises(ValidationError):
            rebuild(repo, archive, job.build_id, {}, "missing-parser")
        with pytest.raises(ValidationError):
            rebuild(repo, archive, "missing", {}, "missing")
        changed = ContractHistoryProcessor(parser_version="historical-contract-json-2")
        with pytest.raises(ValidationError):
            rebuild(repo, archive, job.build_id, {"SYNTH_HIST": changed}, "wrong-policy")
        rebuilt = rebuild(
            repo,
            archive,
            job.build_id,
            {"SYNTH_HIST": changed},
            "new-parser",
            policy=ReprocessPolicy.REPROCESS_WITH_NEW_VERSION,
            clock=ManualClock(2000),
        )
        assert (
            repo.manifest(rebuilt.build_id).content_fingerprint
            != repo.manifest(job.build_id).content_fingerprint
        )
        assert all(f.parser_version == changed.parser_version for f in repo.facts(rebuilt.build_id))
        with pytest.raises(ValidationError):
            rebuild(
                repo,
                archive,
                job.build_id,
                {"SYNTH_HIST": changed},
                "new-parser",
                policy=ReprocessPolicy.REPROCESS_WITH_NEW_VERSION,
            )


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_archive_integrity_failure_is_explicit_without_refetch(tmp_path, fault):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        manifest = codec.dumps(repo.manifest(job.build_id))
        obj = repo.acquisitions(job.build_id)[0].object
        path = archive.root / obj.sha256[:2] / obj.sha256
        path.unlink() if fault == "missing" else path.write_bytes(b"corrupt")
        result = validate_dataset(repo, archive, job.build_id)
        assert (
            result.status is ValidationStatus.INVALID
            and "ARCHIVE_INTEGRITY_FAILURE" in result.errors
        )
        with pytest.raises(PersistenceError):
            rebuild(repo, archive, job.build_id, {"SYNTH_HIST": processor}, "broken-rebuild")
        assert source.requests == 1 and codec.dumps(repo.manifest(job.build_id)) == manifest


def test_metadata_only_build_has_lineage_but_cannot_raw_rebuild(tmp_path):
    job, source, processor = one(retention=RetentionPolicy())
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        assert validate_dataset(repo, archive, job.build_id).status is ValidationStatus.VALID
        assert not list(archive.root.glob("*/*")) and repo.lineage(job.build_id)
        with pytest.raises(PersistenceError):
            rebuild(repo, archive, job.build_id, {"SYNTH_HIST": processor}, "no-bytes")


def test_parse_failure_input_is_retained_and_rebuild_reproduces_gap(tmp_path):
    from eie.backfill.sources import HistoricalPage

    job, source, processor = one(
        {("macro", 10, 20, None): HistoricalPage(b"bad JSON", "https://example.invalid/history")}
    )
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert (
            repo.acquisitions(job.build_id) == ()
            and len(repo.failed_acquisitions(job.build_id)) == 1
        )
        assert archive.read(repo.failed_acquisitions(job.build_id)[0].object) == b"bad JSON"
        assert repo.gaps(job.build_id)[0].reason is GapReason.PARSE_FAILURE
        result = rebuild(
            repo,
            archive,
            job.build_id,
            {"SYNTH_HIST": processor},
            "parse-gap",
            clock=ManualClock(2000),
        )
        assert (
            repo.manifest(job.build_id).content_fingerprint
            == repo.manifest(result.build_id).content_fingerprint
        )


def test_compatible_extension_reuses_frozen_partitions_and_only_fetches_new_range(tmp_path):
    job, source, processor = fixture()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        original = codec.dumps(repo.manifest(job.build_id))
        target = BackfillJob(
            replace(job.definition, interval=TimeRange(10, 25)), "extension", job.build_id
        )
        extended_source = FixtureHistoricalSource(
            source.spec, {("macro", 20, 25, None): page(item(22, "new"))}
        )
        coordinator = BackfillCoordinator(
            repo, archive, ((extended_source, processor),), clock=ManualClock(2000)
        )
        coordinator.prepare(target)
        assert reuse_completed(repo, archive, job.build_id, target, 2000) == 2
        assert reuse_completed(repo, archive, job.build_id, target, 2000) == 0
        assert coordinator.run(target) is JobStatus.COMPLETED and extended_source.requests == 1
        assert len(repo.facts(target.build_id)) == 3
        assert validate_dataset(repo, archive, target.build_id).status is ValidationStatus.VALID
        assert codec.dumps(repo.manifest(job.build_id)) == original
        with pytest.raises(ValidationError):
            reuse_completed(repo, archive, job.build_id, job, 2000)
        wrong = BackfillJob(
            replace(target.definition, event_families=("EARNINGS",)), "wrong", job.build_id
        )
        with pytest.raises(ValidationError):
            reuse_completed(repo, archive, job.build_id, wrong, 2000)


def test_missing_partitions_and_incomplete_finalization_fail(tmp_path, monkeypatch):
    job, source, processor = fixture()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.prepare(job)
        with pytest.raises(PersistenceError):
            finalize(repo, job, 1000)
        saved = repo.partitions(job.build_id)
        monkeypatch.setattr(repo, "partitions", lambda build: saved[:1])
        with pytest.raises(PersistenceError):
            finalize(repo, job, 1000)


def test_registration_context_versions_and_bounds_are_pinned(tmp_path):
    job, source, processor = one()
    with workspace(tmp_path) as (repo, archive, _):
        with pytest.raises(ValidationError):
            BackfillCoordinator(repo, archive, ((source, processor), (source, processor)))
        with pytest.raises(ValidationError):
            BackfillCoordinator(
                repo, archive, ((source, ContractHistoryProcessor(parser_version="bad")),)
            )
        runtime = BackfillCoordinator(repo, archive, ((source, processor),))
        for changed in (
            replace(job.definition, engine_version="0.1.0"),
            replace(job.definition, sources=(replace(source.spec, parser_version="wrong"),)),
        ):
            with pytest.raises(ValidationError):
                runtime.prepare(BackfillJob(changed))
        archive.max_object_bytes = 1
        with pytest.raises(ValidationError):
            runtime.prepare(job)
        with pytest.raises(ValidationError):
            runtime.run(job, max_pages=0)


@pytest.mark.parametrize("status", ["COMPLETED", "KNOWN_EMPTY", "SPLIT", "GAPPED"])
def test_finalization_refuses_unlinked_terminal_claims(tmp_path, status):
    from eie.backfill.coordinator import partition_content
    from eie.backfill.models import PartitionStatus

    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.prepare(job)
        repo.transition(job.build_id, JobStatus.RUNNING)
        part, state = repo.partitions(job.build_id)[0]
        with repo.store.transaction():
            repo.update_progress(
                job.build_id,
                replace(
                    state,
                    status=PartitionStatus(status),
                    content_fingerprint=partition_content(repo, job.build_id, part),
                ),
                0,
            )
        with pytest.raises(PersistenceError):
            finalize(repo, job, 1000)
        assert repo.manifest(job.build_id) is None


def test_same_version_output_drift_is_failed_before_manifest_freeze(tmp_path):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        original = codec.dumps(repo.manifest(job.build_id))
        normalize = processor.normalize
        processor.normalize = lambda *args: tuple(
            replace(f, record=replace(f.record, title="Unversioned code drift"))
            for f in normalize(*args)
        )
        with pytest.raises(PersistenceError, match="disagrees"):
            rebuild(
                repo,
                archive,
                job.build_id,
                {"SYNTH_HIST": processor},
                "drift",
                clock=ManualClock(2000),
            )
        failed = BackfillJob(job.definition, "drift", job.build_id)
        assert repo.status(failed.build_id) is JobStatus.FAILED
        assert repo.manifest(failed.build_id) is None
        assert codec.dumps(repo.manifest(job.build_id)) == original
