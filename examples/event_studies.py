"""Offline historical earnings studies, prior descriptors and deterministic restart."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import CoverageClaim, HistoricalCapability, PartitionPolicy, TimeRange
from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
from eie.core.serialization import canonical_json_bytes
from eie.datasets.models import BackfillJob
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.events.identity import EventIdentity
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from eie.studies.aggregation import DistributionQuery, cohort, historical_distribution
from eie.studies.decay import class_decay, decay
from eie.studies.engine import StudyEngine
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketBar,
    MarketSeriesIdentity,
    MarketSession,
    MarketSlice,
    ReturnType,
    Timeframe,
)
from eie.studies.models import AnchorSemantics, EventWindow, SessionPolicy, StudyPolicy
from eie.studies.repository import StudyRepository
from eie.studies.validation import StudyValidationStatus, validate_study
from examples.historical_rebuild import fixture, item

MINUTE = 60 * 10**9
DAY = 86400 * 10**9
EARNINGS = EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE")


def history(
    root: Path,
    days: tuple[int, ...] = (3, 5, 7, 9),
    *,
    issuer: str = "issuer_fixture",
    clock_offset: int = 0,
) -> tuple[SQLiteEventStore, DatasetReader, tuple[str, ...]]:
    job, _, processor = fixture()
    spec = replace(
        job.definition.sources[0],
        resources=("earnings",),
        capability=HistoricalCapability(max_range_ns=20 * DAY),
    )
    inputs = []
    ids = []
    for d in days:
        at = clock_offset + d * DAY + 10 * MINUTE
        value = item(at, f"earnings-{d}")
        event = replace(
            value.record,
            identity=EventIdentity(
                EARNINGS, EventScope(ScopeKind.COMPANY, (issuer,)), f"earnings-{d}"
            ),
            published_at_ns=at,
        )
        ids.append(event.event_id)
        inputs.append(
            replace(
                value, record=event, fragment_hash=sha256(canonical_json_bytes(event)).hexdigest()
            )
        )
    # Independent co-occurrence in the same source page/bundle.
    overlap = item(clock_offset + days[-1] * DAY + 10 * MINUTE, "management-overlap")
    other = replace(
        overlap.record,
        identity=EventIdentity(
            EventType(EventFamily.MANAGEMENT_GOVERNANCE, "MANAGEMENT.CEO_DEPARTURE"),
            EventScope(ScopeKind.COMPANY, (issuer,)),
            "management-overlap",
        ),
        published_at_ns=overlap.source_available_ns,
    )
    inputs.append(
        replace(
            overlap, record=other, fragment_hash=sha256(canonical_json_bytes(other)).hexdigest()
        )
    )
    interval = TimeRange(clock_offset + DAY, clock_offset + 15 * DAY)
    job = BackfillJob(
        replace(
            job.definition,
            interval=interval,
            sources=(spec,),
            partition_policy=PartitionPolicy(20 * DAY),
        )
    )
    source = FixtureHistoricalSource(
        spec,
        {
            ("earnings", interval.start_ns, interval.end_ns, None): HistoricalPage(
                codec.dumps(tuple(inputs)),
                "https://example.invalid/earnings",
                coverage=CoverageClaim.EXHAUSTIVE,
            )
        },
    )
    store = SQLiteEventStore(root / "studies.sqlite")
    repository = DatasetRepository(store)
    with FileArchive(root / "archive") as archive:
        BackfillCoordinator(
            repository, archive, ((source, processor),), clock=ManualClock(clock_offset + 20 * DAY)
        ).run(job)
    return store, DatasetReader(repository, job.build_id), tuple(ids)


def market(days: int = 15, instrument: str = "security_fixture") -> MarketSlice:
    bars = []
    sessions = []
    for day in range(1, days):
        opening = day * DAY
        sessions.append(
            MarketSession(str(day), opening, opening + 30 * MINUTE, 0, "SYNTHETIC", "calendar-1")
        )
        for minute in range(30):
            price = 100 + day / 10 + minute / 100
            response = {10: 0, 11: 2, 12: 1, 13: 0.5, 14: 0.2}.get(minute, 0)
            close = price + response
            bars.append(
                MarketBar(
                    instrument,
                    opening + minute * MINUTE,
                    opening + (minute + 1) * MINUTE,
                    price,
                    max(price, close),
                    min(price, close),
                    close,
                    100 + minute,
                    opening + (minute + 1) * MINUTE,
                    "SYNTHETIC",
                    Timeframe.M1,
                )
            )
    spec = MarketSeriesIdentity(
        instrument,
        "EQUITY",
        "SYNTHETIC",
        "synthetic-market",
        sha256(canonical_json_bytes(tuple(bars))).hexdigest(),
        "1",
        Timeframe.M1,
        Adjustment.SPLIT_ADJUSTED,
        ReturnType.PRICE_RETURN,
    )
    return MarketSlice(spec, tuple(bars), sessions=tuple(sessions))


def demo(root: Path) -> dict[str, object]:
    store, reader, ids = history(root)
    try:
        supplied = IndexedMarketProvider((market(),))
        window = EventWindow(
            AnchorSemantics.PUBLICATION,
            4 * MINUTE,
            5 * MINUTE,
            Timeframe.M1,
            SessionPolicy.ELAPSED,
            (-4 * MINUTE, 0, MINUTE, 2 * MINUTE, 3 * MINUTE, 4 * MINUTE, 5 * MINUTE),
        )
        policy = StudyPolicy(contamination_pre_ns=MINUTE, contamination_post_ns=5 * MINUTE)
        results = []
        for d, event_id in zip((3, 5, 7, 9), ids, strict=True):
            at = d * DAY + 10 * MINUTE
            engine = StudyEngine(reader, supplied, at + 5 * MINUTE)
            result = engine.study_event(
                event_id,
                "security_fixture",
                engine.anchor(event_id, AnchorSemantics.PUBLICATION),
                window,
                data_as_of_ns=at + 5 * MINUTE,
                policy=policy,
            )
            assert validate_study(result, reader).status is not StudyValidationStatus.INVALID
            assert codec.dumps(codec.loads(codec.dumps(result))) == codec.dumps(result)
            results.append(result)
        repository = StudyRepository(store)
        run = repository.append_run(tuple(results))
        current = results[-1]
        query = DistributionQuery(
            current.anchor_ns,
            reader.build_id,
            EARNINGS.code,
            cohort(current),
            2 * MINUTE,
            2,
            exclude_event_id=current.definition.event.event_id,
        )
        distribution = historical_distribution(tuple(results), query)
        assert distribution.count == 3
        assert current.contamination
        single, typical = decay(current), class_decay(tuple(results), query)
        study_id, output = current.study_id, codec.dumps(current)
        path = root / "studies.sqlite"
    finally:
        store.close()
    with SQLiteEventStore(path) as restarted:
        loaded = StudyRepository(restarted).get(study_id)
        assert loaded is not None and codec.dumps(loaded) == output
    return {
        "study_count": len(results),
        "prior_count": distribution.count,
        "contamination_count": len(current.contamination),
        "result_fingerprint": current.result_fingerprint,
        "single_half_response_ns": single.half_response_time_ns,
        "class_half_response_ns": typical.half_response_time_ns,
        "restart_byte_identical": True,
        "run_id": run.run_id,
    }


if __name__ == "__main__":
    import json

    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), sort_keys=True))
