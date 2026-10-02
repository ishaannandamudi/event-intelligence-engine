"""Bounded fair page scheduling, cancellation and data/progress atomicity."""

from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event

from eie.archive.models import ArchiveAcquisition
from eie.archive.storage import FileArchive
from eie.backfill.models import (
    CoverageClaim,
    GapReason,
    GapRecord,
    JobStatus,
    Partition,
    PartitionStatus,
    ProvenanceKind,
)
from eie.backfill.planner import plan, split
from eie.backfill.sources import (
    HistoricalGapError,
    HistoricalProcessor,
    HistoricalSource,
    logical_identity,
)
from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.datasets.models import BackfillJob, HistoricalFact
from eie.datasets.repository import DatasetRepository
from eie.events.models import CanonicalEvent
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.runtime.clock import RuntimeClock, SystemClock
from eie.runtime.config import positive_int
from eie.scheduled.models import ScheduleRevision
from eie.transport.errors import AcquisitionCancelled, TransientTransportError
from eie.version import __version__

TERMINAL = frozenset(
    (
        PartitionStatus.COMPLETED,
        PartitionStatus.KNOWN_EMPTY,
        PartitionStatus.GAPPED,
        PartitionStatus.SPLIT,
        PartitionStatus.FAILED,
    )
)


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    worker_count: int = 1
    max_attempts_per_page: int = 3

    def __post_init__(self) -> None:
        positive_int(self.worker_count, 8)
        positive_int(self.max_attempts_per_page, 10)


class BackfillCoordinator:
    def __init__(
        self,
        repository: DatasetRepository,
        archive: FileArchive,
        registrations: tuple[tuple[HistoricalSource, HistoricalProcessor], ...],
        *,
        config: BackfillConfig | None = None,
        clock: RuntimeClock | None = None,
        fault: Callable[[str], None] | None = None,
        expected_content_fingerprint: str | None = None,
    ) -> None:
        self.repository, self.archive, self.config = repository, archive, config or BackfillConfig()
        self.clock, self.fault = clock or SystemClock(), fault
        self.expected_content_fingerprint = expected_content_fingerprint
        self._stop, self._cancel = Event(), Event()
        self.providers: dict[str, tuple[HistoricalSource, HistoricalProcessor]] = {}
        for source, processor in registrations:
            if source.spec.provider_id in self.providers or (
                source.spec.adapter_version,
                source.spec.parser_version,
            ) != (processor.adapter_version, processor.parser_version):
                raise ValidationError(
                    "unique historical registration with pinned versions required"
                )
            self.providers[source.spec.provider_id] = source, processor

    def prepare(self, job: BackfillJob) -> None:
        if job.definition.engine_version != __version__:
            raise ValidationError("backfill writes require the pinned current engine version")
        for spec in job.definition.sources:
            if (
                spec.provider_id not in self.providers
                or self.providers[spec.provider_id][0].spec != spec
            ):
                raise ValidationError("historical registration does not match dataset definition")
            if (
                getattr(self.providers[spec.provider_id][1], "identity_bindings", ())
                != spec.identity_bindings
            ):
                raise ValidationError(
                    "historical processor identity context disagrees with definition"
                )
            if spec.capability.max_payload_bytes > self.archive.max_object_bytes:
                raise ValidationError("archive bound must cover historical transport bound")
        self.repository.create(job, plan(job))

    def request_pause(self) -> None:
        self._stop.set()

    def request_cancel(self) -> None:
        self._cancel.set()
        self._stop.set()

    def boundary(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)

    def run(self, job: BackfillJob, *, max_pages: int | None = None) -> JobStatus:
        try:
            return self._run(job, max_pages=max_pages)
        except Exception:
            try:
                if (
                    self.repository.job(job.build_id) is not None
                    and self.repository.status(job.build_id) is JobStatus.RUNNING
                ):
                    self.repository.transition(job.build_id, JobStatus.FAILED)
            except PersistenceError:
                pass  # Preserve the original global failure if the database is unavailable.
            raise

    def _run(self, job: BackfillJob, *, max_pages: int | None = None) -> JobStatus:
        if max_pages is not None:
            positive_int(max_pages, 1_000_000)
        self.prepare(job)
        stored = self.repository.job(job.build_id)
        assert stored is not None
        job = stored
        status = self.repository.status(job.build_id)
        if status in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_GAPS):
            return status
        self._stop.clear()
        self._cancel.clear()
        with self.repository.store.transaction():
            self.repository.transition(job.build_id, JobStatus.RUNNING)
            for part, progress in self.repository.partitions(job.build_id):
                if progress.status is PartitionStatus.FAILED:
                    self.repository.clear_retryable_gaps(job.build_id, part.partition_id)
                    self.repository.update_progress(
                        job.build_id,
                        replace(progress, status=PartitionStatus.PENDING, failures=0),
                        progress.pages,
                    )
        queues: dict[str, deque[Partition]] = {
            s.provider_id: deque() for s in job.definition.sources
        }
        for partition, progress in sorted(
            self.repository.partitions(job.build_id),
            key=lambda pair: (
                pair[0].provider_id,
                pair[0].resource_id,
                pair[0].interval.start_ns,
                pair[0].interval.end_ns,
                pair[0].depth,
            ),
        ):
            if progress.status not in TERMINAL:
                queues[partition.provider_id].append(partition)
        rotation = deque(sorted(queues))
        admitted = 0
        with ThreadPoolExecutor(
            max_workers=self.config.worker_count, thread_name_prefix="eie-history"
        ) as pool:
            while any(queues.values()) and not self._stop.is_set():
                capacity = (
                    self.config.worker_count
                    if max_pages is None
                    else min(self.config.worker_count, max_pages - admitted)
                )
                if capacity == 0:
                    break
                batch = []
                for _ in range(capacity):
                    for _ in range(len(rotation)):
                        provider = rotation[0]
                        rotation.rotate(-1)
                        if queues[provider]:
                            batch.append(queues[provider].popleft())
                            break
                futures = [pool.submit(self.process_page, job, p) for p in batch]
                admitted += len(batch)
                for partition, future in zip(batch, futures, strict=True):
                    children = future.result()
                    queues[partition.provider_id].extend(children)
                    if self.repository.progress(partition.partition_id).status not in TERMINAL:
                        queues[partition.provider_id].append(partition)
        if self._cancel.is_set():
            final = JobStatus.CANCELLED
        elif self._stop.is_set() or any(queues.values()):
            final = JobStatus.PAUSED
        elif any(
            p.status is PartitionStatus.FAILED for _, p in self.repository.partitions(job.build_id)
        ):
            final = JobStatus.FAILED
        else:
            from eie.datasets.builder import finalize

            return finalize(
                self.repository,
                job,
                self.clock.utc_ns(),
                expected_content_fingerprint=self.expected_content_fingerprint,
            ).status
        self.repository.transition(job.build_id, final)
        return final

    def gap(
        self, job: BackfillJob, part: Partition, reason: GapReason, *, retryable: bool = False
    ) -> None:
        self.repository.add_gap(
            job.build_id,
            GapRecord(
                part.partition_id,
                part.provider_id,
                part.resource_id,
                part.interval,
                reason,
                self.clock.utc_ns(),
                retryable,
            ),
        )

    def process_page(self, job: BackfillJob, part: Partition) -> tuple[Partition, ...]:
        source, processor = self.providers[part.provider_id]
        cap = source.spec.capability
        prior = self.repository.progress(part.partition_id)
        if prior.status in TERMINAL:
            return ()
        if self._stop.is_set():
            return ()
        with self.repository.store.transaction():
            prior = self.repository.progress(part.partition_id)
            self.repository.update_progress(
                job.build_id,
                replace(prior, status=PartitionStatus.RUNNING, attempts=prior.attempts + 1),
                prior.pages,
            )
        unsupported = (
            not cap.supported
            or part.interval.start_ns < cap.earliest_ns
            or (cap.latest_ns is not None and part.interval.end_ns > cap.latest_ns)
        )
        if unsupported or not cap.point_in_time:
            with self.repository.store.transaction():
                self.gap(
                    job,
                    part,
                    GapReason.UNSUPPORTED_HISTORY
                    if unsupported
                    else GapReason.NO_POINT_IN_TIME_HISTORY,
                )
                self.repository.update_progress(
                    job.build_id,
                    replace(
                        self.repository.progress(part.partition_id),
                        status=PartitionStatus.GAPPED,
                        content_fingerprint=partition_content(self.repository, job.build_id, part),
                    ),
                    prior.pages,
                )
            return ()
        stage = "fetch"
        started = self.clock.monotonic_ns()
        acquisition: ArchiveAcquisition | None = None
        try:
            if prior.pages >= cap.max_pages:
                raise ValidationError("historical page count exhausted")
            page = source.fetch(part, prior.cursor, self._stop)
            acquired = (
                self.clock.utc_ns()
                if page.original_acquired_ns is None
                else page.original_acquired_ns
            )
            self.boundary("after_fetch")
            if len(page.body) > cap.max_payload_bytes:
                raise ValidationError("historical payload exceeds source bound")
            stage = "archive"
            obj = self.archive.write(page.body, job.definition.archive_policy)
            self.boundary("after_archive")
            acquisition = ArchiveAcquisition(
                job.build_id,
                part.partition_id,
                part.provider_id,
                part.resource_id,
                prior.pages + 1,
                page.source_reference,
                acquired,
                obj,
                page.content_type,
                job.definition.archive_policy,
                processor.adapter_version,
                processor.parser_version,
                page.next_cursor,
                page.coverage,
                ProvenanceKind.HISTORICALLY_RECONSTRUCTED
                if page.original_acquired_ns is None
                else ProvenanceKind.ARCHIVE_REPLAYED,
                None if page.original_acquired_ns is None else self.clock.utc_ns(),
                page.too_many_results,
            )
            stage = "cursor"
            if page.next_cursor is not None and (
                page.next_cursor == prior.cursor
                or page.next_cursor in prior.visited_cursors
                or any(
                    a.object.sha256 == obj.sha256
                    for a in self.repository.acquisitions(job.build_id, part.partition_id)
                )
            ):
                raise ValidationError("historical pagination does not advance")
            stage = "parse"
            children = split(part, cap.max_split_depth) if page.too_many_results else ()
            if children and (
                prior.pages
                or len(self.repository.partitions(job.build_id)) + len(children)
                > job.definition.partition_policy.max_partitions
            ):
                raise ValidationError("historical split exceeds policy or follows committed pages")
            facts = (
                ()
                if children
                else processor.normalize(page, part, acquired, job.definition.settings)
            )
            if len(facts) > cap.max_items:
                raise ValidationError("historical result count exceeds policy")
            if any(
                f.provider_id != part.provider_id
                or f.logical_id != logical_identity(f.record)
                or f.resource_id != part.resource_id
                or f.raw.source.source_class is not source.spec.source_class
                or (f.adapter_version, f.parser_version)
                != (processor.adapter_version, processor.parser_version)
                for f in facts
            ):
                raise ValidationError("historical facts disagree with trusted source registration")
            facts = tuple(f for f in facts if selected_record(f, job))
            self.boundary("after_normalize")
            stage = "persist"
            with self.repository.store.transaction():
                current = self.repository.progress(part.partition_id)
                if current.pages != prior.pages or current.cursor != prior.cursor:
                    raise PersistenceError("concurrent historical page conflict")
                created, duplicates = self.repository.append_page(
                    job, part, acquisition, facts, current
                )
                self.boundary("after_data")
                status = PartitionStatus.SPLIT if children else PartitionStatus.RUNNING
                if page.next_cursor is None and not children:
                    status = PartitionStatus.COMPLETED
                    if page.coverage is not CoverageClaim.EXHAUSTIVE:
                        status = PartitionStatus.GAPPED
                        reason = {
                            CoverageClaim.PARTIAL: GapReason.PARTIAL_PROVIDER_COVERAGE,
                            CoverageClaim.UNSUPPORTED: GapReason.UNSUPPORTED_HISTORY,
                        }.get(page.coverage, GapReason.UNKNOWN_COMPLETENESS)
                        self.gap(job, part, reason)
                    elif prior.items_seen + len(facts) == 0:
                        status = PartitionStatus.KNOWN_EMPTY
                if any(
                    QualityFlag.ENTITY_UNRESOLVED in getattr(f.record, "quality_flags", ())
                    for f in facts
                ):
                    self.gap(job, part, GapReason.ENTITY_UNRESOLVED)
                if any(f.source_available_ns is None for f in facts):
                    self.gap(job, part, GapReason.TEMPORAL_AMBIGUITY)
                visited = prior.visited_cursors + (() if prior.cursor is None else (prior.cursor,))
                updated = replace(
                    current,
                    status=status,
                    cursor=page.next_cursor,
                    visited_cursors=visited,
                    pages=prior.pages + 1,
                    items_seen=prior.items_seen + len(facts),
                    items_committed=prior.items_committed + created,
                    duplicates=prior.duplicates + duplicates,
                    failures=0,
                )
                if status in TERMINAL:
                    updated = replace(
                        updated,
                        content_fingerprint=partition_content(self.repository, job.build_id, part),
                    )
                for child in children:
                    if (
                        len(self.repository.partitions(job.build_id))
                        >= job.definition.partition_policy.max_partitions
                    ):
                        raise ValidationError("dynamic partition bound exhausted")
                    self.repository.add_partition(job.build_id, child)
                self.boundary("before_progress")
                self.repository.update_progress(job.build_id, updated, prior.pages)
                self.boundary("before_commit")
            self.boundary("after_commit")
            self.repository.operation(
                job.build_id,
                {
                    "provider": part.provider_id,
                    "partition": part.partition_id,
                    "status": "committed",
                    "bytes": len(page.body),
                    "hash": obj.sha256,
                    "duration_ns": self.clock.monotonic_ns() - started,
                },
            )
            return children
        except Exception as exc:
            if isinstance(exc, AcquisitionCancelled) and self._stop.is_set():
                return ()
            if stage == "persist":
                raise  # Global persistence/conflict failures cannot become a successful manifest.
            retryable = isinstance(exc, TransientTransportError)
            reason = (
                GapReason.SCHEMA_MISMATCH
                if isinstance(exc, ProviderSchemaMismatchError)
                else GapReason.PARSE_FAILURE
                if isinstance(exc, ProviderParseError)
                else GapReason.SOURCE_UNAVAILABLE
                if stage == "fetch"
                else GapReason.PAGINATION_FAILURE
                if stage == "cursor"
                else GapReason.ARCHIVE_FAILURE
                if stage == "archive"
                else GapReason.PARSE_FAILURE
            )
            if isinstance(exc, HistoricalGapError):
                reason, retryable = exc.reason, False
            elif isinstance(exc, ValidationError) and stage == "fetch":
                reason, retryable = GapReason.PAGINATION_FAILURE, False
            with self.repository.store.transaction():
                current = self.repository.progress(part.partition_id)
                failures = current.failures + 1
                status = (
                    PartitionStatus.RUNNING
                    if retryable and failures < self.config.max_attempts_per_page
                    else PartitionStatus.FAILED
                    if retryable
                    else PartitionStatus.GAPPED
                )
                if acquisition is not None:
                    self.repository.archive_metadata(acquisition, failed=True)
                if status in TERMINAL:
                    self.gap(job, part, reason, retryable=retryable)
                self.repository.update_progress(
                    job.build_id,
                    replace(
                        current,
                        status=status,
                        failures=failures,
                        content_fingerprint=partition_content(self.repository, job.build_id, part)
                        if status in TERMINAL
                        else None,
                    ),
                    current.pages,
                )
            self.repository.operation(
                job.build_id,
                {
                    "provider": part.provider_id,
                    "partition": part.partition_id,
                    "status": "failed",
                    "category": reason.value,
                    "retryable": retryable,
                },
            )
            return ()


def partition_content(repository: DatasetRepository, build_id: str, part: Partition) -> str:
    return fingerprint(
        "eie:partition-content:v1",
        (
            part.provider_id,
            part.resource_id,
            part.interval,
            tuple(
                f.content_fingerprint
                for f in repository.facts(build_id, partition_id=part.partition_id)
            ),
            tuple(
                (a.object.sha256, a.next_cursor, a.coverage, a.too_many_results)
                for a in repository.acquisitions(build_id, part.partition_id)
                + repository.failed_acquisitions(build_id, part.partition_id)
            ),
        ),
    )


def selected_record(fact: HistoricalFact, job: BackfillJob) -> bool:
    record = fact.record.event if isinstance(fact.record, ScheduleRevision) else fact.record
    if not isinstance(record, CanonicalEvent):
        return (
            True  # Retain domain context and evidence even when canonical event views are filtered.
        )
    definition = job.definition
    return (
        not definition.event_families
        or record.identity.event_type.family.value in definition.event_families
    ) and (
        not definition.entity_universe
        or bool(set(record.identity.scope.identifiers) & set(definition.entity_universe))
    )
