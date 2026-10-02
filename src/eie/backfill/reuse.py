"""Explicit reuse of frozen verified inputs for a compatible extension build."""

from dataclasses import replace

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import partition_content
from eie.backfill.models import PartitionStatus, ProvenanceKind
from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.models import BackfillJob
from eie.datasets.repository import DatasetRepository
from eie.ingestion.models import RetentionMode


def reuse_completed(
    repository: DatasetRepository,
    archive: FileArchive,
    original_build: str,
    target: BackfillJob,
    replayed_ns: int,
) -> int:
    original = repository.manifest(original_build)
    if (
        original is None
        or original_build == target.build_id
        or target.parent_build_id != original_build
    ):
        raise ValidationError("reuse requires a distinct child of a finalized build")
    old, new = original.job.definition, target.definition
    if (
        old.sources,
        old.settings,
        old.partition_policy,
        old.archive_policy,
        old.event_families,
        old.entity_universe,
        old.engine_version,
    ) != (
        new.sources,
        new.settings,
        new.partition_policy,
        new.archive_policy,
        new.event_families,
        new.entity_universe,
        new.engine_version,
    ):
        raise ValidationError(
            "partition reuse requires identical transformation and source policies"
        )
    sources = {
        (p.provider_id, p.resource_id, p.interval): (p, state)
        for p, state in repository.partitions(original_build)
        if state.status in (PartitionStatus.COMPLETED, PartitionStatus.KNOWN_EMPTY)
    }
    reused = 0
    for part, progress in repository.partitions(target.build_id):
        previous = sources.get((part.provider_id, part.resource_id, part.interval))
        if previous is None or progress.status is not PartitionStatus.PENDING:
            continue
        old_part, old_progress = previous
        if old_progress.content_fingerprint != partition_content(
            repository, original_build, old_part
        ):
            raise PersistenceError("reused partition integrity failure")
        with repository.store.transaction():
            for acquisition in repository.acquisitions(original_build, old_part.partition_id):
                if acquisition.retention.mode is not RetentionMode.BOUNDED_RAW:
                    raise PersistenceError("verified partition reuse requires retained raw inputs")
                archive.read(acquisition.object)
                copied = replace(
                    acquisition,
                    build_id=target.build_id,
                    partition_id=part.partition_id,
                    provenance=ProvenanceKind.ARCHIVE_REPLAYED,
                    replayed_at_ns=replayed_ns,
                )
                repository.append_page(
                    target,
                    part,
                    copied,
                    repository.facts_for_acquisition(original_build, acquisition.acquisition_id),
                    progress,
                )
            repository.update_progress(
                target.build_id, replace(old_progress, partition_id=part.partition_id), 0
            )
        reused += 1
    return reused
