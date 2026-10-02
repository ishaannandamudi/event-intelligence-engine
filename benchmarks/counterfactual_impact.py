"""Offline distinct-event daily-window workload and separate stage timings."""

import argparse
import json
from dataclasses import replace
from hashlib import sha256
from math import sin
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
from eie.impact.abnormal import abnormal_curve, cumulative_returns
from eie.impact.engine import ImpactEngine
from eie.impact.fitting import fit_model
from eie.impact.inputs import ControlCandidate, ImpactInputs
from eie.impact.models import CARWindow, EstimationWindow, MatchingPolicy, ModelType
from eie.impact.repository import ImpactRepository
from eie.impact.validation import ImpactValidationStatus, validate_impact
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
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
from examples.counterfactual_impact import feature
from examples.event_studies import DAY, EARNINGS
from examples.historical_rebuild import fixture, item


def profile(root: Path, count: int = 1000) -> dict[str, object]:
    if not 1 <= count <= 10_000:
        raise ValueError("profile event count must be 1..10000")
    original, _, processor = fixture()
    width = 400 * DAY
    parts = (count + 49) // 50
    interval = TimeRange(300 * DAY, 300 * DAY + parts * width)
    source_spec = replace(
        original.definition.sources[0],
        resources=("earnings",),
        capability=HistoricalCapability(max_range_ns=width),
    )
    job = BackfillJob(
        replace(
            original.definition,
            interval=interval,
            sources=(source_spec,),
            partition_policy=PartitionPolicy(width),
        )
    )
    pages = {}
    ids = []
    for part in range(parts):
        left = interval.start_ns + part * width
        records = []
        for j in range(part * 50, min(count, (part + 1) * 50)):
            at = (300 + 8 * j) * DAY
            v = item(at, f"impact-profile-{j}")
            event = replace(
                v.record,
                identity=EventIdentity(
                    EARNINGS,
                    EventScope(ScopeKind.COMPANY, ("issuer_profile",)),
                    f"impact-profile-{j}",
                ),
                published_at_ns=at,
            )
            records.append(
                replace(
                    v, record=event, fragment_hash=sha256(canonical_json_bytes(event)).hexdigest()
                )
            )
            ids.append(event.event_id)
        pages[("earnings", left, left + width, None)] = HistoricalPage(
            codec.dumps(tuple(records)),
            "https://example.invalid/impact-profile",
            coverage=CoverageClaim.EXHAUSTIVE,
        )
    end = (300 + 8 * count + 6) * DAY
    session = MarketSession("synthetic-continuous-daily", 49 * DAY, end, 0, "SYNTHETIC", "1")

    def series(instrument, beta):
        bars = []
        close = previous_market = 100.0
        for j in range(50, 300 + 8 * count + 6):
            market_close = 100 + sin(j * 0.31)
            market_return = market_close / previous_market - 1
            previous_market = market_close
            opening, close = (
                close,
                close * (1 + 0.00001 + beta * market_return + 0.0001 * sin(j * 0.71)),
            )
            bars.append(
                MarketBar(
                    instrument,
                    (j - 1) * DAY,
                    j * DAY,
                    opening,
                    max(opening, close),
                    min(opening, close),
                    close,
                    100,
                    j * DAY,
                    "SYNTHETIC",
                    Timeframe.DAILY,
                )
            )
        identity = MarketSeriesIdentity(
            instrument,
            "SYNTHETIC_GENERIC",
            "SYNTHETIC",
            "profile-" + instrument,
            sha256(canonical_json_bytes(tuple(bars))).hexdigest(),
            "1",
            Timeframe.DAILY,
            Adjustment.SPLIT_ADJUSTED,
            ReturnType.PRICE_RETURN,
        )
        return MarketSlice(identity, tuple(bars), sessions=(session,))

    provider = IndexedMarketProvider(
        tuple(
            series(i, beta)
            for i, beta in (
                ("target", 1.5),
                ("market", 1),
                ("sector", 0.8),
                ("control_a", 1.2),
                ("control_b", 1.4),
                ("control_c", 1.8),
            )
        )
    )
    source = FixtureHistoricalSource(source_spec, pages)
    with (
        SQLiteEventStore(root / "impact.sqlite") as store,
        FileArchive(root / "archive") as archive,
    ):
        dataset_repo = DatasetRepository(store)
        BackfillCoordinator(
            dataset_repo, archive, ((source, processor),), clock=ManualClock(end + DAY)
        ).run(job)
        reader = DatasetReader(dataset_repo, job.build_id)
        studies = StudyEngine(reader, provider, end)
        engine = ImpactEngine()
        window = EventWindow(
            AnchorSemantics.PUBLICATION,
            DAY,
            5 * DAY,
            Timeframe.DAILY,
            SessionPolicy.ELAPSED,
            (0, DAY, 2 * DAY, 3 * DAY, 4 * DAY, 5 * DAY),
        )
        estimation = EstimationWindow(-250 * DAY, -20 * DAY, 19 * DAY, SessionPolicy.ELAPSED)
        cars = (CARWindow("post", 0, 5 * DAY), CARWindow("short", 0, DAY))
        policy = StudyPolicy(
            volume_baseline_ns=0, max_lag_ns=DAY, contamination_pre_ns=0, contamination_post_ns=0
        )
        stored_sample = []
        market_seconds = sector_seconds = control_seconds = study_seconds = 0.0
        sector_count = control_count = 0
        stride = max(1, count // 100)
        for j, event_id in enumerate(ids):
            at = (300 + 8 * j) * DAY
            started = perf_counter()
            study = studies.study_event(
                event_id,
                "target",
                studies.anchor(event_id, AnchorSemantics.PUBLICATION),
                window,
                data_as_of_ns=end,
                policy=policy,
            )
            study_seconds += perf_counter() - started
            target = provider.read("target", at - 251 * DAY, at + 5 * DAY)
            benchmark = provider.read("market", at - 251 * DAY, at + 5 * DAY)
            base = ImpactInputs(study, target, benchmark)
            started = perf_counter()
            r = engine.impact_event(base, ModelType.MARKET_MODEL, estimation, cars)
            market_seconds += perf_counter() - started
            assert r.fit.observation_count == 230
            if j % stride == 0 and len(stored_sample) < 100:
                stored_sample.append(r)
                sector = replace(
                    base, benchmark=provider.read("sector", at - 251 * DAY, at + 5 * DAY)
                )
                started = perf_counter()
                engine.impact_event(sector, ModelType.SECTOR_ADJUSTED, estimation, cars)
                sector_seconds += perf_counter() - started
                sector_count += 1
                controls = ImpactInputs(
                    study,
                    target,
                    target_features=feature("target", at, 10),
                    controls=tuple(
                        ControlCandidate(
                            i, provider.read(i, at - 251 * DAY, at + 5 * DAY), feature(i, at, size)
                        )
                        for i, size in (("control_a", 9), ("control_b", 11), ("control_c", 20))
                    ),
                )
                started = perf_counter()
                engine.impact_event(
                    controls,
                    ModelType.MATCHED_CONTROLS,
                    estimation,
                    cars,
                    matching=MatchingPolicy(k=2),
                )
                control_seconds += perf_counter() - started
                control_count += 1
        sample = stored_sample[0]
        started = perf_counter()
        for _ in stored_sample:
            fit_model(sample.inputs, sample.model_spec)
        fitting_seconds = perf_counter() - started
        curve = abnormal_curve(sample.inputs, sample.model_spec, sample.fit)
        started = perf_counter()
        for _ in stored_sample:
            cumulative_returns(sample.inputs, sample.model_spec, sample.fit, curve)
        car_seconds = perf_counter() - started
        started = perf_counter()
        for value in stored_sample:
            assert validate_impact(value, reader).status is not ImpactValidationStatus.INVALID
        validation_seconds = perf_counter() - started
        repository = ImpactRepository(store)
        started = perf_counter()
        run = repository.append_run(tuple(stored_sample))
        persistence_seconds = perf_counter() - started
        started = perf_counter()
        loaded = repository.impacts_as_of(end + 1, event_id=stored_sample[-1].event_id)
        query_seconds = perf_counter() - started
        assert len(loaded) == 1 and codec.dumps(loaded[0]) == codec.dumps(stored_sample[-1])
        assert repository.run(run.run_id) == run
        return {
            "distinct_events": count,
            "market_models": count,
            "sector_models": sector_count,
            "matched_models": control_count,
            "matching_candidates": 3,
            "estimation_periods": 230,
            "timeframe": "daily",
            "car_windows": 2,
            "raw_study_seconds": study_seconds,
            "market_model_seconds": market_seconds,
            "market_models_per_second": count / market_seconds,
            "sector_seconds": sector_seconds,
            "control_matching_and_estimation_seconds": control_seconds,
            "stage_profile_samples": len(stored_sample),
            "fitting_seconds": fitting_seconds,
            "fitting_per_second": len(stored_sample) / fitting_seconds,
            "car_generation_seconds": car_seconds,
            "validation_seconds": validation_seconds,
            "persisted_results": len(stored_sample),
            "persistence_seconds": persistence_seconds,
            "indexed_query_seconds": query_seconds,
            "database_bytes_including_history_and_wal": sum(
                p.stat().st_size for p in root.glob("impact.sqlite*")
            ),
            "all_stored_fingerprints_verified": True,
            "workload": "distinct synthetic events, continuous daily supplied calendar; "
            "bounded storage sample; no CI timing threshold",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=1000)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        print(json.dumps(profile(Path(directory), args.events), indent=2, sort_keys=True))
