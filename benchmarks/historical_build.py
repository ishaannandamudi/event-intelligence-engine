"""Offline append/index build profile; wall times are observations, never CI gates."""

import argparse
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import CoverageClaim, JobStatus, PartitionPolicy, TimeRange
from eie.backfill.planner import plan
from eie.backfill.rebuild import rebuild
from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
from eie.datasets.builder import finalize
from eie.datasets.models import BackfillJob, ValidationStatus
from eie.datasets.repository import DatasetRepository
from eie.datasets.validation import validate_dataset
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from examples.historical_rebuild import fixture, item


def profile(root: Path, partitions: int = 200, items_per_partition: int = 50) -> dict[str, object]:
    original, _, processor = fixture()
    definition = replace(
        original.definition,
        interval=TimeRange(10, 10 + partitions * 5),
        partition_policy=PartitionPolicy(5),
    )
    job = BackfillJob(definition)
    started = perf_counter()
    planned = plan(job)
    planning_seconds = perf_counter() - started
    pages = {
        (p.resource_id, p.interval.start_ns, p.interval.end_ns, None): HistoricalPage(
            codec.dumps(
                tuple(
                    item(p.interval.start_ns + 2, f"synthetic-{i}-{n}")
                    for n in range(items_per_partition)
                )
            ),
            "https://example.invalid/history",
            coverage=CoverageClaim.EXHAUSTIVE,
        )
        for i, p in enumerate(planned)
    }
    source = FixtureHistoricalSource(definition.sources[0], pages)
    clock = ManualClock(1_000_000_000)
    with (
        FileArchive(root / "archive") as archive,
        SQLiteEventStore(root / "history.sqlite") as store,
    ):
        repository = DatasetRepository(store)
        runtime = BackfillCoordinator(repository, archive, ((source, processor),), clock=clock)
        runtime.prepare(job)
        repository.transition(job.build_id, JobStatus.RUNNING)
        started = perf_counter()
        for part in planned:
            runtime.process_page(job, part)
        build_seconds = perf_counter() - started
        started = perf_counter()
        manifest = finalize(repository, job, clock.utc_ns())
        manifest_seconds = perf_counter() - started
        started = perf_counter()
        assert validate_dataset(repository, archive, job.build_id).status is ValidationStatus.VALID
        validation_seconds = perf_counter() - started
        started = perf_counter()
        rebuilt = rebuild(
            repository,
            archive,
            job.build_id,
            {source.spec.provider_id: processor},
            "profile-rebuild",
            clock=clock,
        )
        rebuild_seconds = perf_counter() - started
        replay = repository.manifest(rebuilt.build_id)
        assert replay is not None and replay.content_fingerprint == manifest.content_fingerprint
        assert (
            validate_dataset(repository, archive, rebuilt.build_id).status is ValidationStatus.VALID
        )
        objects = [path for path in archive.root.glob("*/*") if path.is_file()]
        archive_bytes = sum(path.stat().st_size for path in objects)
        database_bytes = sum(path.stat().st_size for path in root.glob("history.sqlite*"))
        return {
            "partitions": partitions,
            "source_items": partitions * items_per_partition,
            "normalized_records": sum(
                p.items_committed for _, p in repository.partitions(job.build_id)
            ),
            "archive_objects": len(objects),
            "archive_bytes": archive_bytes,
            "database_bytes_with_rebuild_and_wal": database_bytes,
            "planning_seconds": planning_seconds,
            "page_build_seconds": build_seconds,
            "items_per_second": partitions * items_per_partition / build_seconds,
            "manifest_seconds": manifest_seconds,
            "validation_seconds": validation_seconds,
            "rebuild_seconds": rebuild_seconds,
            "rebuild_matches": True,
            "workload": "synthetic audited event pages; SQLite + filesystem archive",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--partitions", type=int, default=200)
    parser.add_argument("--items", type=int, default=50)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        print(
            json.dumps(
                profile(Path(directory), args.partitions, args.items), indent=2, sort_keys=True
            )
        )
