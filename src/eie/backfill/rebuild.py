"""Explicit isolated rebuild from committed archive inputs, never automatic refetch."""

from dataclasses import replace
from threading import Event

from eie.archive.models import ArchiveAcquisition
from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import GapReason, Partition, SourceSpec
from eie.backfill.sources import HistoricalGapError, HistoricalPage, HistoricalProcessor
from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.models import BackfillJob, ReprocessPolicy
from eie.datasets.repository import DatasetRepository
from eie.ingestion.models import RetentionMode
from eie.runtime.clock import RuntimeClock
from eie.version import __version__


class ArchiveHistoricalSource:
    def __init__(
        self,
        spec: SourceSpec,
        archive: FileArchive,
        pages: dict[tuple[str, int, int], tuple[ArchiveAcquisition, ...]],
        gaps: dict[tuple[str, int, int], GapReason] | None = None,
    ) -> None:
        self.spec, self.archive, self.pages = spec, archive, pages
        self.gaps = gaps or {}

    def fetch(self, partition: Partition, cursor: str | None, stop: Event) -> HistoricalPage:
        if stop.is_set():
            raise ValidationError("archive replay stopped")
        pages = self.pages.get(
            (partition.resource_id, partition.interval.start_ns, partition.interval.end_ns), ()
        )
        prior = None
        for acquisition in pages:
            if prior == cursor:
                if acquisition.retention.mode is not RetentionMode.BOUNDED_RAW:
                    raise PersistenceError("archive retention does not permit raw rebuild")
                return HistoricalPage(
                    self.archive.read(acquisition.object),
                    acquisition.source_reference,
                    acquisition.content_type,
                    acquisition.next_cursor,
                    acquisition.coverage,
                    acquisition.too_many_results,
                    acquisition.acquired_at_ns,
                )
            prior = acquisition.next_cursor
        key = (partition.resource_id, partition.interval.start_ns, partition.interval.end_ns)
        if key in self.gaps:
            raise HistoricalGapError(self.gaps[key])
        raise PersistenceError("archive page chain is incomplete")


def rebuild(
    repository: DatasetRepository,
    archive: FileArchive,
    original_build: str,
    processors: dict[str, HistoricalProcessor],
    build_version: str,
    *,
    policy: ReprocessPolicy = ReprocessPolicy.REBUILD_SAME_VERSION,
    clock: RuntimeClock | None = None,
) -> BackfillJob:
    original = repository.manifest(original_build)
    if original is None or policy is ReprocessPolicy.NO_REPROCESS:
        raise ValidationError("explicit finalized source build and rebuild policy required")
    registrations = []
    specs = []
    changed = False
    for old_spec in original.job.definition.sources:
        processor = processors.get(old_spec.provider_id)
        if processor is None:
            raise ValidationError("pinned historical processor unavailable")
        same = (processor.adapter_version, processor.parser_version) == (
            old_spec.adapter_version,
            old_spec.parser_version,
        )
        changed |= not same
        if policy is ReprocessPolicy.REBUILD_SAME_VERSION and not same:
            raise ValidationError("rebuild policy disagrees with parser/adapter versions")
        spec = replace(
            old_spec,
            adapter_version=processor.adapter_version,
            parser_version=processor.parser_version,
        )
        pages = {}
        gaps = {}
        for part, _ in repository.partitions(original_build):
            if part.provider_id == spec.provider_id:
                acquisitions = repository.acquisitions(
                    original_build, part.partition_id
                ) + repository.failed_acquisitions(original_build, part.partition_id)
                for gap in original.gaps:
                    if gap.partition_id == part.partition_id:
                        gaps[(part.resource_id, part.interval.start_ns, part.interval.end_ns)] = (
                            gap.reason
                        )
                for acquisition in acquisitions:
                    if acquisition.retention.mode is not RetentionMode.BOUNDED_RAW:
                        raise PersistenceError(
                            "metadata-only inputs cannot be deterministically rebuilt"
                        )
                    archive.read(
                        acquisition.object
                    )  # Refuse corruption before creating a new build.
                pages[(part.resource_id, part.interval.start_ns, part.interval.end_ns)] = (
                    acquisitions
                )
        specs.append(spec)
        registrations.append((ArchiveHistoricalSource(spec, archive, pages, gaps), processor))
    if policy is ReprocessPolicy.REPROCESS_WITH_NEW_VERSION and not changed:
        raise ValidationError("new-version policy requires a changed parser or adapter")
    definition = replace(
        original.job.definition,
        sources=tuple(specs),
        engine_version=__version__
        if policy is ReprocessPolicy.REPROCESS_WITH_NEW_VERSION
        else original.job.definition.engine_version,
    )
    job = BackfillJob(definition, build_version, original_build)
    if job.build_id == original_build or repository.job(job.build_id) is not None:
        raise ValidationError("rebuild requires a new isolated build identity")
    BackfillCoordinator(
        repository,
        archive,
        tuple(registrations),
        clock=clock,
        expected_content_fingerprint=original.content_fingerprint
        if policy is ReprocessPolicy.REBUILD_SAME_VERSION
        else None,
    ).run(job)
    return job
