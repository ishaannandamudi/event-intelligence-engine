"""Validation detects damaged metadata, frozen payloads and graph relationships."""

from dataclasses import replace

import pytest
from historical_helpers import item, one, page, workspace

from eie.backfill.models import PartitionStatus
from eie.datasets.models import ValidationStatus
from eie.datasets.validation import validate_dataset
from eie.persistence import codec


@pytest.mark.parametrize(
    "case,expected",
    [
        ("counts", "INVALID_MANIFEST_COUNTS"),
        ("digest", "INVALID_DATASET_CONTENT"),
        ("partition-digest", "INVALID_PARTITION_MANIFEST_CONTENT"),
        ("completeness", "INVALID_COMPLETENESS_REPORT"),
        ("archive-hashes", "ARCHIVE_MANIFEST_MISMATCH"),
        ("lineage-reference", "INVALID_LINEAGE_REFERENCE"),
        ("lineage-context", "INVALID_LINEAGE_CONTEXT"),
        ("orphan", "ORPHAN_NORMALIZED_RECORD"),
        ("fact-index", "INVALID_FACT_INDEX"),
        ("fact-context", "INVALID_FACT_CONTEXT"),
        ("archive-metadata", "INVALID_ARCHIVE_METADATA"),
        ("acquisition-context", "INVALID_ACQUISITION_CONTEXT"),
        ("page-chain", "INVALID_PAGE_CHAIN"),
        ("cursor", "INVALID_CURSOR_CHAIN"),
        ("incomplete-chain", "INCOMPLETE_PAGE_CHAIN"),
        ("incomplete", "INCOMPLETE_PARTITION"),
        ("progress-hash", "INVALID_PARTITION_CONTENT"),
        ("partition-context", "INVALID_PARTITION_CONTEXT"),
        ("bad-codec", "INVALID_STORED_CONTRACT"),
    ],
)
def test_explicit_damage_returns_invalid_without_mutation_or_refetch(tmp_path, case, expected):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        manifest = repo.manifest(job.build_id)
        if case in ("counts", "digest", "partition-digest", "completeness", "archive-hashes"):
            changes = {
                "counts": {"record_counts": tuple(sorted((*manifest.record_counts, ("extra", 1))))},
                "digest": {"records_fingerprint": "a" * 64},
                "partition-digest": {"partitions_fingerprint": "a" * 64},
                "completeness": {
                    "completeness": replace(manifest.completeness, reconstruction_quality=())
                },
                "archive-hashes": {"archive_hashes": ()},
            }[case]
            damaged = replace(manifest, **changes)
            repo.store._connection.execute("DROP TRIGGER immutable_manifest_update")
            repo.store._connection.execute(
                "UPDATE dataset_manifests SET payload=?", (codec.dumps(damaged),)
            )
        elif case.startswith("lineage"):
            lineage = repo.lineage(job.build_id)[0]
            damaged = replace(
                lineage,
                **(
                    {"acquisition_id": "nonexistent"}
                    if case == "lineage-reference"
                    else {"settings_fingerprint": "wrong"}
                ),
            )
            repo.store._connection.execute("DROP TRIGGER frozen_historical_lineage_update")
            repo.store._connection.execute(
                "UPDATE historical_lineage SET payload=?", (codec.dumps(damaged),)
            )
        elif case == "orphan":
            repo.store._connection.execute("DROP TRIGGER frozen_historical_lineage_delete")
            repo.store._connection.execute("DELETE FROM historical_lineage")
        elif case.startswith("fact"):
            repo.store._connection.execute("DROP TRIGGER frozen_historical_facts_update")
            if case == "fact-index":
                repo.store._connection.execute("UPDATE historical_facts SET revision=99")
            else:
                damaged = replace(repo.facts(job.build_id)[0], parser_version="changed")
                repo.store._connection.execute(
                    "UPDATE historical_facts SET payload=?", (codec.dumps(damaged),)
                )
        elif case == "archive-metadata":
            acquisition = repo.acquisitions(job.build_id)[0]
            repo.store._connection.execute(
                "UPDATE archive_objects SET payload=?",
                (codec.dumps(replace(acquisition.object, byte_length=1)),),
            )
        elif case in ("acquisition-context", "page-chain", "incomplete-chain"):
            acquisition = repo.acquisitions(job.build_id)[0]
            damaged = replace(
                acquisition,
                **(
                    {"provider_id": "OTHER"}
                    if case == "acquisition-context"
                    else {"page_number": 2}
                    if case == "page-chain"
                    else {"next_cursor": "more"}
                ),
            )
            repo.store._connection.execute("DROP TRIGGER frozen_historical_acquisitions_update")
            repo.store._connection.execute(
                "UPDATE historical_acquisitions SET payload=?", (codec.dumps(damaged),)
            )
        elif case == "bad-codec":
            repo.store._connection.execute("DROP TRIGGER frozen_historical_facts_update")
            repo.store._connection.execute(
                "UPDATE historical_facts SET payload=?", (b"broken typed JSON",)
            )
        else:
            part, state = repo.partitions(job.build_id)[0]
            repo.store._connection.execute("DROP TRIGGER frozen_historical_partitions_update")
            if case == "partition-context":
                repo.store._connection.execute(
                    "UPDATE historical_partitions SET payload=?",
                    (codec.dumps(replace(part, job_id="other")),),
                )
            else:
                damaged = replace(
                    state,
                    **(
                        {"cursor": "wrong"}
                        if case == "cursor"
                        else {"status": PartitionStatus.PENDING}
                        if case == "incomplete"
                        else {"content_fingerprint": "a" * 64}
                    ),
                )
                repo.store._connection.execute(
                    "UPDATE historical_partitions SET progress=?", (codec.dumps(damaged),)
                )
        saved = repo.store._connection.total_changes
        result = validate_dataset(repo, archive, job.build_id)
        assert result.status is ValidationStatus.INVALID and expected in result.errors
        assert source.requests == 1 and repo.store._connection.total_changes == saved


def test_missing_partition_split_child_and_invalid_parent_are_detected(tmp_path, monkeypatch):
    pages = {
        ("macro", 10, 20, None): page(split=True),
        ("macro", 10, 15, None): page(item(12, "a")),
        ("macro", 15, 20, None): page(item(17, "b")),
    }
    job, source, processor = one(pages)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        parts = repo.partitions(job.build_id)
        parent = next(pair for pair in parts if pair[1].status is PartitionStatus.SPLIT)
        child = next(pair for pair in parts if pair[0].parent_id is not None)
        monkeypatch.setattr(
            repo, "partitions", lambda _: tuple(pair for pair in parts if pair != child)
        )
        assert "MISSING_SPLIT_CHILD" in validate_dataset(repo, archive, job.build_id).errors
        monkeypatch.setattr(
            repo, "partitions", lambda _: tuple(pair for pair in parts if pair != parent)
        )
        errors = validate_dataset(repo, archive, job.build_id).errors
        assert "MISSING_PARTITION" in errors and "INVALID_SPLIT_LINEAGE" in errors
        monkeypatch.setattr(
            repo,
            "partitions",
            lambda _: (
                *parts,
                (replace(parent[0], interval=type(parent[0].interval)(20, 30)), parent[1]),
            ),
        )
        assert "UNPLANNED_PARTITION" in validate_dataset(repo, archive, job.build_id).errors


def test_database_integrity_failures_are_included(tmp_path, monkeypatch):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        monkeypatch.setattr(repo.store, "validate", lambda: ("FOREIGN_KEY_VIOLATION",))
        assert "DATABASE_INTEGRITY_FAILURE" in validate_dataset(repo, archive, job.build_id).errors
