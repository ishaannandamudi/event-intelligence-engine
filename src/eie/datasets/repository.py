"""Append/index dataset storage on EIE's serialized SQLite transaction boundary."""

from collections.abc import Iterator
from dataclasses import replace
from threading import get_ident
from typing import TypeVar, cast

from eie.archive.models import ArchiveAcquisition, ArchiveObject
from eie.backfill.models import (
    GapRecord,
    JobStatus,
    Partition,
    PartitionProgress,
    PartitionStatus,
    ProvenanceKind,
)
from eie.core.errors import PersistenceError
from eie.datasets.models import BackfillJob, DatasetManifest, HistoricalFact, LineageRecord
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.sources.models import RawSourceObservation

T = TypeVar("T")


def typed(payload: object, cls: type[T]) -> T:
    value = codec.loads(cast(bytes, payload))
    if not isinstance(value, cls):
        raise PersistenceError("historical stored contract type mismatch")
    return value


class DatasetRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def owned(self) -> None:
        if self.store._depth == 0 or self.store._owner != get_ident():
            raise PersistenceError("historical mutation requires linked transaction")

    def mutable(self, build_id: str) -> None:
        if self.manifest(build_id) is not None:
            raise PersistenceError("completed dataset build is immutable")

    def create(self, job: BackfillJob, partitions: tuple[Partition, ...]) -> None:
        with self.store.transaction():
            previous = self.job(job.build_id)
            if previous is not None:
                if (
                    previous.definition.semantic_content() != job.definition.semantic_content()
                    or previous.build_version != job.build_version
                    or previous.parent_build_id != job.parent_build_id
                ):
                    raise PersistenceError("historical job identity collision")
                return
            self.store._connection.execute(
                "INSERT INTO historical_jobs VALUES (?,?,?,?)",
                (job.build_id, job.job_id, JobStatus.PLANNED.value, codec.dumps(job)),
            )
            for partition in partitions:
                self.add_partition(job.build_id, partition)

    def job(self, build_id: str) -> BackfillJob | None:
        rows = self.store._rows("SELECT payload FROM historical_jobs WHERE build_id=?", (build_id,))
        return None if not rows else typed(rows[0][0], BackfillJob)

    def status(self, build_id: str) -> JobStatus:
        rows = self.store._rows("SELECT status FROM historical_jobs WHERE build_id=?", (build_id,))
        if not rows:
            raise PersistenceError("unknown historical build")
        return JobStatus(cast(str, rows[0][0]))

    def transition(self, build_id: str, status: JobStatus) -> None:
        allowed = {
            JobStatus.PLANNED: {JobStatus.RUNNING, JobStatus.CANCELLED},
            JobStatus.RUNNING: {
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.COMPLETED,
                JobStatus.COMPLETED_WITH_GAPS,
            },
            JobStatus.PAUSED: {JobStatus.RUNNING, JobStatus.CANCELLED},
            JobStatus.FAILED: {JobStatus.RUNNING, JobStatus.CANCELLED},
        }
        with self.store.transaction():
            self.mutable(build_id)
            if status not in allowed.get(self.status(build_id), set()):
                raise PersistenceError("invalid historical job transition")
            self.store._connection.execute(
                "UPDATE historical_jobs SET status=? WHERE build_id=?", (status.value, build_id)
            )

    def add_partition(self, build_id: str, partition: Partition) -> None:
        self.owned()
        self.mutable(build_id)
        self.store._connection.execute(
            "INSERT INTO historical_partitions VALUES (?,?,?,?)",
            (
                partition.partition_id,
                build_id,
                codec.dumps(partition),
                codec.dumps(PartitionProgress(partition.partition_id)),
            ),
        )

    def partitions(self, build_id: str) -> tuple[tuple[Partition, PartitionProgress], ...]:
        return tuple(
            (typed(p, Partition), typed(progress, PartitionProgress))
            for p, progress in self.store._rows(
                "SELECT payload,progress FROM historical_partitions WHERE build_id=? ORDER BY id",
                (build_id,),
            )
        )

    def progress(self, partition_id: str) -> PartitionProgress:
        rows = self.store._rows(
            "SELECT progress FROM historical_partitions WHERE id=?", (partition_id,)
        )
        if not rows:
            raise PersistenceError("unknown historical partition")
        return typed(rows[0][0], PartitionProgress)

    def update_progress(self, build_id: str, value: PartitionProgress, expected_pages: int) -> None:
        self.owned()
        self.mutable(build_id)
        if not self.store._rows(
            "SELECT id FROM historical_partitions WHERE id=? AND build_id=?",
            (value.partition_id, build_id),
        ):
            raise PersistenceError("partition does not belong to this build")
        old = self.progress(value.partition_id)
        if old.pages != expected_pages or old.status in (
            PartitionStatus.COMPLETED,
            PartitionStatus.KNOWN_EMPTY,
            PartitionStatus.GAPPED,
            PartitionStatus.SPLIT,
        ):
            raise PersistenceError("historical progress conflict or immutable partition")
        if value.pages < old.pages or value.pages != len(
            self.acquisitions(build_id, value.partition_id)
        ):
            raise PersistenceError("historical progress requires durable linked pages")
        if value.items_seen != value.items_committed + value.duplicates:
            raise PersistenceError("historical progress item counts disagree")
        self.store._connection.execute(
            "UPDATE historical_partitions SET progress=? WHERE id=? AND build_id=?",
            (codec.dumps(value), value.partition_id, build_id),
        )

    def acquisitions(
        self, build_id: str, partition_id: str | None = None
    ) -> tuple[ArchiveAcquisition, ...]:
        return tuple(
            typed(r[0], ArchiveAcquisition)
            for r in self.store._rows(
                "SELECT payload FROM historical_acquisitions WHERE build_id=?"
                + (" AND partition_id=?" if partition_id is not None else "")
                + " ORDER BY partition_id,page",
                (build_id,) if partition_id is None else (build_id, partition_id),
            )
        )

    def archive_metadata(self, acquisition: ArchiveAcquisition, *, failed: bool = False) -> None:
        self.owned()
        self.mutable(acquisition.build_id)
        self.store._connection.execute(
            "INSERT OR IGNORE INTO archive_objects VALUES (?,?)",
            (acquisition.object.sha256, codec.dumps(acquisition.object)),
        )
        obj = self.store._rows(
            "SELECT payload FROM archive_objects WHERE hash=?", (acquisition.object.sha256,)
        )[0][0]
        if typed(obj, ArchiveObject) != acquisition.object:
            raise PersistenceError("archive metadata collision")
        if failed:
            self.store._connection.execute(
                "INSERT OR IGNORE INTO historical_failed_acquisitions VALUES (?,?,?,?,?)",
                (
                    acquisition.acquisition_id,
                    acquisition.build_id,
                    acquisition.partition_id,
                    acquisition.object.sha256,
                    codec.dumps(acquisition),
                ),
            )
        else:
            self.store._connection.execute(
                "INSERT INTO historical_acquisitions VALUES (?,?,?,?,?,?)",
                (
                    acquisition.acquisition_id,
                    acquisition.build_id,
                    acquisition.partition_id,
                    acquisition.page_number,
                    acquisition.object.sha256,
                    codec.dumps(acquisition),
                ),
            )

    def failed_acquisitions(
        self, build_id: str, partition_id: str | None = None
    ) -> tuple[ArchiveAcquisition, ...]:
        return tuple(
            typed(r[0], ArchiveAcquisition)
            for r in self.store._rows(
                "SELECT payload FROM historical_failed_acquisitions WHERE build_id=?"
                + (" AND partition_id=?" if partition_id is not None else "")
                + " ORDER BY partition_id,id",
                (build_id,) if partition_id is None else (build_id, partition_id),
            )
        )

    def append_page(
        self,
        job: BackfillJob,
        partition: Partition,
        acquisition: ArchiveAcquisition,
        facts: tuple[HistoricalFact, ...],
        progress: PartitionProgress,
    ) -> tuple[int, int]:
        # Caller also updates progress in this same transaction. No normalization/network here.
        self.owned()
        self.mutable(job.build_id)
        if (
            partition.job_id,
            acquisition.build_id,
            acquisition.partition_id,
            acquisition.provider_id,
            acquisition.resource_id,
            progress.partition_id,
        ) != (
            job.job_id,
            job.build_id,
            partition.partition_id,
            partition.provider_id,
            partition.resource_id,
            partition.partition_id,
        ):
            raise PersistenceError("historical page context disagrees with linked job")
        self.archive_metadata(acquisition)
        created = duplicates = 0
        for fact in facts:
            prior_live = self.store.get_observation(fact.raw.observation_id)
            if prior_live is not None:
                fact = replace(fact, raw=prior_live, provenance=ProvenanceKind.LIVE_OBSERVED)
            elif acquisition.provenance is ProvenanceKind.ARCHIVE_REPLAYED:
                fact = replace(fact, provenance=ProvenanceKind.ARCHIVE_REPLAYED)
            rows = self.store._rows(
                "SELECT payload FROM historical_observations WHERE build_id=? AND id=?",
                (job.build_id, fact.raw.observation_id),
            )
            if rows:
                fact = replace(fact, raw=typed(rows[0][0], RawSourceObservation))
            else:
                self.store._connection.execute(
                    "INSERT INTO historical_observations VALUES (?,?,?)",
                    (job.build_id, fact.raw.observation_id, codec.dumps(fact.raw)),
                )
            present = self.store._rows(
                "SELECT payload FROM historical_facts WHERE build_id=? AND id=?",
                (job.build_id, fact.fact_id),
            )
            if present:
                if (
                    typed(present[0][0], HistoricalFact).content_fingerprint
                    != fact.content_fingerprint
                ):
                    raise PersistenceError("historical fact identity collision")
                duplicates += 1
            else:
                self.store._connection.execute(
                    "INSERT INTO historical_facts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        job.build_id,
                        fact.fact_id,
                        fact.provider_id,
                        fact.resource_id,
                        fact.logical_id,
                        fact.source_revision,
                        fact.source_available_ns,
                        type(fact.record).__name__,
                        fact.raw.observation_id,
                        codec.dumps(fact),
                    ),
                )
                created += 1
            lineage = LineageRecord(
                job.build_id,
                partition.partition_id,
                acquisition.acquisition_id,
                fact.fact_id,
                fact.raw.observation_id,
                acquisition.object.sha256,
                fact.adapter_version,
                fact.parser_version,
                job.definition.settings.fingerprint,
            )
            self.store._connection.execute(
                "INSERT OR IGNORE INTO historical_lineage VALUES (?,?,?,?,?)",
                (
                    job.build_id,
                    fact.fact_id,
                    partition.partition_id,
                    acquisition.acquisition_id,
                    codec.dumps(lineage),
                ),
            )
        return created, duplicates

    def facts(
        self, build_id: str, *, partition_id: str | None = None
    ) -> tuple[HistoricalFact, ...]:
        if partition_id is None:
            rows = self.store._rows(
                "SELECT payload FROM historical_facts WHERE build_id=? ORDER BY id", (build_id,)
            )
        else:
            rows = self.store._rows(
                "SELECT DISTINCT f.id,f.payload FROM historical_facts f JOIN historical_lineage l "
                "ON f.build_id=l.build_id AND f.id=l.fact_id WHERE f.build_id=? AND l.p"
                "artition_id=? ORDER BY f.id",
                (build_id, partition_id),
            )
            rows = [(r[1],) for r in rows]
        return tuple(typed(r[0], HistoricalFact) for r in rows)

    def iter_facts(
        self,
        build_id: str,
        *,
        as_of_ns: int | None = None,
        kind: str | None = None,
        provider: str | None = None,
        resource: str | None = None,
    ) -> Iterator[HistoricalFact]:
        clauses, parameters = ["build_id=?"], [build_id]
        if as_of_ns is not None:
            clauses.append("available<=?")
        for column, value in (("kind", kind), ("provider", provider), ("resource", resource)):
            if value is not None:
                clauses.append(column + "=?")
        arguments: list[object] = list(parameters)
        if as_of_ns is not None:
            arguments.append(as_of_ns)
        arguments.extend(value for value in (kind, provider, resource) if value is not None)
        with self.store._lock:
            self.store._check()
            for row in self.store._connection.execute(
                "SELECT payload FROM historical_facts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id",
                arguments,
            ):
                yield typed(row[0], HistoricalFact)

    def facts_for_acquisition(
        self, build_id: str, acquisition_id: str
    ) -> tuple[HistoricalFact, ...]:
        return tuple(
            typed(r[0], HistoricalFact)
            for r in self.store._rows(
                "SELECT f.payload FROM historical_facts f JOIN historical_lineage l "
                "ON f.build_id=l.build_id AND f.id=l.fact_id WHERE f.build_id=? "
                "AND l.acquisition_id=? ORDER BY f.id",
                (build_id, acquisition_id),
            )
        )

    def lineage(self, build_id: str) -> tuple[LineageRecord, ...]:
        return tuple(
            typed(r[0], LineageRecord)
            for r in self.store._rows(
                "SELECT payload FROM historical_lineage WHERE build_id=? ORDER BY fact_"
                "id,partition_id,acquisition_id",
                (build_id,),
            )
        )

    def add_gap(self, build_id: str, gap: GapRecord) -> None:
        self.owned()
        self.mutable(build_id)
        self.store._connection.execute(
            "INSERT OR IGNORE INTO historical_gaps VALUES (?,?,?,?)",
            (build_id, gap.gap_id, gap.partition_id, codec.dumps(gap)),
        )

    def gaps(self, build_id: str) -> tuple[GapRecord, ...]:
        return tuple(
            typed(r[0], GapRecord)
            for r in self.store._rows(
                "SELECT payload FROM historical_gaps WHERE build_id=? ORDER BY id", (build_id,)
            )
        )

    def clear_retryable_gaps(self, build_id: str, partition_id: str) -> None:
        self.owned()
        self.mutable(build_id)
        for gap in self.gaps(build_id):
            if gap.partition_id == partition_id and gap.retryable:
                self.store._connection.execute(
                    "DELETE FROM historical_gaps WHERE build_id=? AND id=?", (build_id, gap.gap_id)
                )

    def manifest(self, build_id: str) -> DatasetManifest | None:
        rows = self.store._rows(
            "SELECT payload FROM dataset_manifests WHERE build_id=?", (build_id,)
        )
        return None if not rows else typed(rows[0][0], DatasetManifest)

    def operation(self, build_id: str, value: object) -> None:
        with self.store.transaction():
            self.store._connection.execute(
                "INSERT INTO historical_operations(build_id,payload) VALUES (?,?)",
                (build_id, codec.dumps(value)),
            )
