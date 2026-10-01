"""Offline workload profile; measured throughput is descriptive, never a CI gate."""

import json
import statistics
import time
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.quality import ConfirmationState
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US
from eie.persistence.memory import InMemoryEventStore
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import SystemClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.coordinator import IngestionCoordinator
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass
from examples.runtime_replay import provider

COUNT, BATCH, POLLS = 20_000, 250, 100


def workload() -> tuple[tuple[RawSourceObservation, CanonicalEvent], ...]:
    source = SourceIdentity("BENCHMARK", "synthetic-1", "synthetic-1", SourceClass.OFFICIAL_PRIMARY)
    rows = []
    for i in range(COUNT):
        raw = RawSourceObservation(
            source, f"item-{i}", "1", f"{i:064x}", "application/json", i + 1, i + 1
        )
        event = CanonicalEvent(
            EventIdentity(
                MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), f"benchmark-{i}"
            ),
            EventRevision(1, RevisionKind.ORIGINAL, i + 1),
            LifecycleState.RELEASED,
            ConfirmationState.CONFIRMED_PRIMARY,
            (raw.observation_id,),
            frozenset({SourceClass.OFFICIAL_PRIMARY}),
            "Synthetic profile",
        )
        rows.append((raw, event))
    return tuple(rows)


def profile(
    store: InMemoryEventStore | SQLiteEventStore,
    rows: tuple[tuple[RawSourceObservation, CanonicalEvent], ...],
) -> dict[str, float | int]:
    commits = []
    started = time.perf_counter()
    for offset in range(0, COUNT, BATCH):
        batch_started = time.perf_counter()
        with store.transaction() if isinstance(store, SQLiteEventStore) else nullcontext():
            for raw, event in rows[offset : offset + BATCH]:
                assert store.append_observation(raw) and store.append_event(event)
        commits.append(time.perf_counter() - batch_started)
    elapsed = time.perf_counter() - started
    started = time.perf_counter()
    duplicates = 0
    with store.transaction() if isinstance(store, SQLiteEventStore) else nullcontext():
        for raw, event in rows[-1000:]:
            duplicates += int(not store.append_observation(raw)) + int(
                not store.append_event(event)
            )
    duplicate_seconds = time.perf_counter() - started
    started = time.perf_counter()
    assert tuple(len(store.known_as_of(at)) for at in (100, 10_000, 20_000)) == (
        100,
        10_000,
        20_000,
    )
    return {
        "observations": COUNT,
        "events": COUNT,
        "batch_size": BATCH,
        "ingestion_seconds": round(elapsed, 6),
        "events_per_second": round(COUNT / elapsed, 2),
        "batch_ms_median": round(statistics.median(commits) * 1000, 3),
        "duplicate_operations": duplicates,
        "duplicate_seconds": round(duplicate_seconds, 6),
        "three_replay_queries_seconds": round(time.perf_counter() - started, 6),
    }


def main() -> None:
    rows = workload()
    memory = profile(InMemoryEventStore(), rows)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "profile.sqlite"
        with SQLiteEventStore(path) as store:
            durable = profile(store, rows)
            # Poll a small domain session against the already populated durable event store.
            clock = SystemClock()
            runtime = IngestionCoordinator(
                store,
                (provider(clock),),
                RuntimeConfig(enabled=True, queue_capacity=1),
                clock=clock,
            )
            commit_ns = []
            duplicates = 0
            maximum_queue = 0
            started = time.perf_counter()
            for _ in range(POLLS):
                assert runtime.fetch_once("SEC_EDGAR") is None
                maximum_queue = max(maximum_queue, runtime.queue.depth)
                result = runtime.process_next()
                assert result is not None and result.committed
                duplicates += result.duplicates
                commit_ns.append(result.latencies.commit_ns)
            polling = {
                "polls": POLLS,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "duplicates": duplicates,
                "queue_capacity": 1,
                "maximum_queue": maximum_queue,
                "commit_ms_median": round(statistics.median(commit_ns) / 1_000_000, 3),
                "checkpoint_generation": store.checkpoint("SEC_EDGAR", "recent").generation,
            }
        with SQLiteEventStore(path) as store:
            assert store.counts()["events"] == COUNT + 1
            assert len(store.known_as_of(COUNT)) == COUNT
            durable["database_bytes"] = path.stat().st_size
        print(
            json.dumps(
                {"memory": memory, "sqlite": durable, "polling": polling}, indent=2, sort_keys=True
            )
        )


if __name__ == "__main__":
    main()
