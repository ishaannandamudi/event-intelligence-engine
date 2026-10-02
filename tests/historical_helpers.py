"""Audited synthetic historical sources, never network acquisition."""

from contextlib import contextmanager
from dataclasses import replace

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import CoverageClaim, HistoricalCapability, PartitionPolicy, TimeRange
from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
from eie.datasets.models import BackfillJob
from eie.datasets.repository import DatasetRepository
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from examples.historical_rebuild import fixture, item

DEFAULT_RANGE = TimeRange(10, 20)


def one(pages=None, *, capability=None, interval=DEFAULT_RANGE, policy=None, retention=None):
    job, _, processor = fixture()
    spec = replace(
        job.definition.sources[0], capability=capability or HistoricalCapability(max_range_ns=10)
    )
    definition = replace(
        job.definition,
        interval=interval,
        sources=(spec,),
        partition_policy=policy or PartitionPolicy(10),
        archive_policy=retention or job.definition.archive_policy,
    )
    source = FixtureHistoricalSource(
        spec, pages or {("macro", 10, 20, None): page(item(12, "cpi"))}
    )
    return BackfillJob(definition), source, processor


def page(*items, cursor=None, coverage=CoverageClaim.EXHAUSTIVE, split=False):
    return HistoricalPage(
        codec.dumps(items),
        "https://example.invalid/history",
        next_cursor=cursor,
        coverage=coverage,
        too_many_results=split,
    )


@contextmanager
def workspace(root, registrations=None, **options):
    with (
        SQLiteEventStore(root / "history.sqlite") as store,
        FileArchive(root / "archive") as archive,
    ):
        repository = DatasetRepository(store)
        runtime = (
            None
            if registrations is None
            else BackfillCoordinator(
                repository, archive, registrations, clock=ManualClock(1000), **options
            )
        )
        yield repository, archive, runtime
