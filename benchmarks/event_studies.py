"""Offline 10k-event supplied-minute-data profile, without CI timing thresholds."""

import argparse
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

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
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from eie.studies.aggregation import DistributionQuery, cohort, historical_distribution
from eie.studies.alignment import align
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
from eie.studies.models import (
    AnchorSemantics,
    BenchmarkType,
    EventWindow,
    SessionPolicy,
    StudyPolicy,
)
from eie.studies.repository import StudyRepository
from examples.event_studies import DAY, EARNINGS, MINUTE
from examples.historical_rebuild import fixture, item


def profile(root: Path, count: int = 10_000) -> dict[str, object]:
    if not 1 <= count <= 10_000:
        raise ValueError("profile count must be 1..10000")
    old, _, processor = fixture()
    width = 150 * MINUTE
    total_parts = (count + 49) // 50
    interval = TimeRange(DAY + 10 * MINUTE, DAY + 10 * MINUTE + total_parts * width)
    spec = replace(
        old.definition.sources[0],
        resources=("earnings",),
        capability=HistoricalCapability(max_range_ns=width),
    )
    job = BackfillJob(
        replace(
            old.definition,
            interval=interval,
            sources=(spec,),
            partition_policy=PartitionPolicy(width),
        )
    )
    ids = []
    pages = {}
    for part in range(total_parts):
        left = DAY + 10 * MINUTE + part * width
        inputs = []
        for j in range(part * 50, min(count, (part + 1) * 50)):
            at = DAY + (3 * j + 10) * MINUTE
            v = item(at, f"earnings-{j}")
            event = replace(
                v.record,
                identity=EventIdentity(
                    EARNINGS, EventScope(ScopeKind.COMPANY, ("issuer_fixture",)), f"earnings-{j}"
                ),
                published_at_ns=at,
            )
            ids.append(event.event_id)
            inputs.append(
                replace(
                    v,
                    range_at_ns=at,
                    record=event,
                    fragment_hash=sha256(canonical_json_bytes(event)).hexdigest(),
                )
            )
        pages[("earnings", left, left + width, None)] = HistoricalPage(
            codec.dumps(tuple(inputs)),
            "https://example.invalid/profile",
            coverage=CoverageClaim.EXHAUSTIVE,
        )
    end = DAY + (3 * count + 20) * MINUTE
    sessions = (MarketSession("synthetic-continuous", DAY, end, 0, "SYNTHETIC", "1"),)

    def series(instrument):
        bars = []
        for minute in range(3 * count + 20):
            price = 100 + (minute % 7) * 0.01
            close = price + (minute % 3) * 0.02
            t = DAY + minute * MINUTE
            bars.append(
                MarketBar(
                    instrument,
                    t,
                    t + MINUTE,
                    price,
                    max(price, close),
                    min(price, close),
                    close,
                    100.0,
                    t + MINUTE,
                    "SYNTHETIC",
                    Timeframe.M1,
                )
            )
        identity = MarketSeriesIdentity(
            instrument,
            "SYNTHETIC_CONTINUOUS",
            "SYNTHETIC",
            "profile-market",
            sha256(canonical_json_bytes(bars)).hexdigest(),
            "1",
            Timeframe.M1,
            Adjustment.SPLIT_ADJUSTED,
            ReturnType.PRICE_RETURN,
        )
        return MarketSlice(identity, tuple(bars), sessions=sessions)

    provider = IndexedMarketProvider((series("asset"), series("sector")))
    source = FixtureHistoricalSource(spec, pages)
    with (
        SQLiteEventStore(root / "profile.sqlite") as store,
        FileArchive(root / "archive") as archive,
    ):
        repository = DatasetRepository(store)
        BackfillCoordinator(
            repository, archive, ((source, processor),), clock=ManualClock(end + DAY)
        ).run(job)
        reader = DatasetReader(repository, job.build_id)
        engine = StudyEngine(reader, provider, end)
        windows = tuple(
            EventWindow(
                AnchorSemantics.PUBLICATION,
                pre * MINUTE,
                post * MINUTE,
                Timeframe.M1,
                SessionPolicy.ELAPSED,
                (-2 * MINUTE, 0, MINUTE, 2 * MINUTE, 3 * MINUTE, 4 * MINUTE, 5 * MINUTE),
            )
            for pre, post in ((3, 5), (4, 6))
        )
        policy = StudyPolicy(
            volume_baseline_ns=0,
            contamination_pre_ns=MINUTE // 2,
            contamination_post_ns=MINUTE // 2,
        )
        started = perf_counter()
        results = tuple(
            engine.study_event(
                event_id,
                "asset",
                engine.anchor(event_id, AnchorSemantics.PUBLICATION),
                windows[i % 2],
                data_as_of_ns=end,
                policy=policy,
                benchmarks=((BenchmarkType.SECTOR_INDEX, "sector"),),
            )
            for i, event_id in enumerate(ids)
        )
        generation = perf_counter() - started
        started = perf_counter()
        for r in results:
            align(
                r.market, r.pre_start_ns, r.anchor_ns, r.post_end_ns, policy, SessionPolicy.ELAPSED
            )
        alignment = perf_counter() - started
        repo = StudyRepository(store)
        started = perf_counter()
        run = repo.append_run(results)
        persistence = perf_counter() - started
        cohort_rows = results[::2]
        query = DistributionQuery(
            end + 1, job.build_id, EARNINGS.code, cohort(cohort_rows[0]), 2 * MINUTE, 1
        )
        started = perf_counter()
        distribution = historical_distribution(cohort_rows, query)
        aggregation = perf_counter() - started
        started = perf_counter()
        for r in results:
            decay(r)
        typical = class_decay(cohort_rows, query)
        decay_seconds = perf_counter() - started
        started = perf_counter()
        loaded = repo.studies_as_of(end + 1, event_id=ids[-1])
        query_seconds = perf_counter() - started
        assert len(loaded) == 1 and loaded[0].result_fingerprint == results[-1].result_fingerprint
        assert distribution.count == len(cohort_rows) and repo.run(run.run_id) == run
        database_bytes = sum(p.stat().st_size for p in root.glob("profile.sqlite*"))
        return {
            "events": count,
            "windows": len(windows),
            "bars_per_instrument": 3 * count + 20,
            "instruments": 2,
            "alignment_seconds": alignment,
            "alignment_per_second": count / alignment,
            "study_generation_seconds": generation,
            "studies_per_second": count / generation,
            "persistence_seconds": persistence,
            "aggregation_seconds": aggregation,
            "decay_seconds": decay_seconds,
            "indexed_query_seconds": query_seconds,
            "database_bytes_including_history_and_wal": database_bytes,
            "prior_distribution_count": distribution.count,
            "class_decay_fingerprint": typical.result_fingerprint,
            "deterministic_stored_output": True,
            "workload": "synthetic continuous minute sessions; two alternating windows, "
            "sector comparison; no timing gate",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10_000)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        print(json.dumps(profile(Path(directory), args.events), indent=2, sort_keys=True))
