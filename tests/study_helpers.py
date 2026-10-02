"""Deterministic supplied-market study fixtures."""

from eie.studies.engine import StudyEngine
from eie.studies.market import IndexedMarketProvider
from eie.studies.models import AnchorSemantics, EventWindow, SessionPolicy, StudyPolicy
from examples.event_studies import DAY, MINUTE, market


def window():
    return EventWindow(
        AnchorSemantics.PUBLICATION,
        4 * MINUTE,
        5 * MINUTE,
        market().identity.timeframe,
        SessionPolicy.ELAPSED,
        (-4 * MINUTE, 0, MINUTE, 2 * MINUTE, 3 * MINUTE, 4 * MINUTE, 5 * MINUTE),
    )


def result(
    reader, event_id, day=9, *, data=None, policy=None, benchmarks=(), context=(), surprise=None
):
    at = day * DAY + 10 * MINUTE
    engine = StudyEngine(reader, IndexedMarketProvider((data or market(),)), at + 5 * MINUTE)
    return engine.study_event(
        event_id,
        "security_fixture",
        engine.anchor(event_id, AnchorSemantics.PUBLICATION),
        window(),
        data_as_of_ns=at + 5 * MINUTE,
        policy=policy or StudyPolicy(contamination_pre_ns=MINUTE, contamination_post_ns=5 * MINUTE),
        benchmarks=benchmarks,
        context=context,
        surprise=surprise,
    )


def inputs_history(root, inputs):
    from dataclasses import replace

    from eie.archive.storage import FileArchive
    from eie.backfill.coordinator import BackfillCoordinator
    from eie.backfill.models import CoverageClaim, HistoricalCapability, PartitionPolicy, TimeRange
    from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
    from eie.datasets.models import BackfillJob
    from eie.datasets.queries import DatasetReader
    from eie.datasets.repository import DatasetRepository
    from eie.persistence import codec
    from eie.persistence.sqlite import SQLiteEventStore
    from eie.runtime.clock import ManualClock
    from examples.historical_rebuild import fixture

    job, _, processor = fixture()
    spec = replace(
        job.definition.sources[0],
        resources=("earnings",),
        capability=HistoricalCapability(max_range_ns=20 * DAY),
    )
    interval = TimeRange(DAY, 15 * DAY)
    job = BackfillJob(
        replace(
            job.definition,
            sources=(spec,),
            interval=interval,
            partition_policy=PartitionPolicy(20 * DAY),
        )
    )
    source = FixtureHistoricalSource(
        spec,
        {
            ("earnings", DAY, 15 * DAY, None): HistoricalPage(
                codec.dumps(inputs),
                "https://example.invalid/events",
                coverage=CoverageClaim.EXHAUSTIVE,
            )
        },
    )
    store = SQLiteEventStore(root / "events.sqlite")
    repo = DatasetRepository(store)
    with FileArchive(root / "archive") as archive:
        BackfillCoordinator(repo, archive, ((source, processor),), clock=ManualClock(20 * DAY)).run(
            job
        )
    return store, DatasetReader(repo, job.build_id)
