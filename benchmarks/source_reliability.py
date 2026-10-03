"""Offline indexed reliability profile, with explicit workload and no CI timing gate."""

import argparse
import cProfile
import json
import pstats
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.evidence.schema import ClaimStatus
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.aggregation import select
from eie.reliability.coverage import calculate as coverage
from eie.reliability.latency import calculate as latency
from eie.reliability.models import (
    CoverageSlot,
    CoverageUniverse,
    ReliabilityQuery,
    SourceReliabilityPolicy,
)
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.reliability import calculate
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import WindowKind
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, ISSUER, SECOND, ingest


def profile(count: int) -> dict[str, object]:
    if not 100 <= count <= 10000:
        raise ValueError("profile workload must contain 100–10,000 observations")
    metrics: dict[str, object] = {"original_observations": count, "providers": 3, "queries": 3}
    profiler = cProfile.Profile()
    profiler.enable()
    with TemporaryDirectory() as directory:
        path = Path(directory) / "source.sqlite"
        with SQLiteEventStore(path) as store:
            repo = ReliabilityRepository(store)
            engine = ReliabilityQueries(repo)
            started = perf_counter()
            corrections = retractions = 0
            for i in range(count):
                name = ("A", "B", "C")[i % 3]
                known = (i + 10) * 3 * SECOND
                first, row = ingest(
                    store, name, known, key=f"release-{i}", publication=known - 5 * SECOND
                )
                if i % 50 == 0:
                    ingest(
                        store,
                        name,
                        known + 2 * SECOND,
                        key=f"release-{i}",
                        prior=first,
                        value=101,
                        status=ClaimStatus.CORRECTED,
                    )
                    corrections += 1
                elif i % 51 == 0:
                    ingest(
                        store,
                        name,
                        known + 2 * SECOND,
                        key=f"release-{i}",
                        prior=first,
                        status=ClaimStatus.RETRACTED,
                    )
                    retractions += 1
            metrics["source_ingestion_and_index_seconds"] = perf_counter() - started
            metrics["corrections"], metrics["retractions"] = corrections, retractions
            policy = SourceReliabilityPolicy(
                window=WindowKind.TRAILING_OBSERVATIONS,
                observations=min(250, count),
                max_observations=5000,
            )
            cutoff = (count + 20) * 3 * SECOND
            queries = tuple(
                ReliabilityQuery(DATASET.dataset_id, name, "financial", cutoff, policy)
                for name in ("A", "B", "C")
            )
            started = perf_counter()
            inputs = tuple(repo.collect(q) for q in queries)
            metrics["indexed_collection_seconds"] = perf_counter() - started
            started = perf_counter()
            selected = tuple(select(q, rows) for q, (rows, _) in zip(queries, inputs, strict=True))
            metrics["prior_filter_seconds"] = perf_counter() - started
            started = perf_counter()
            for rows in selected:
                for row in rows:
                    latency(row, cutoff)
            metrics["latency_extraction_seconds"] = perf_counter() - started
            started = perf_counter()
            for q, (rows, peers) in zip(queries, inputs, strict=True):
                calculate(q, rows, peers)
            metrics["aggregation_seconds"] = perf_counter() - started
            universe = CoverageUniverse(
                DATASET.dataset_id,
                "A",
                "financial",
                ISSUER,
                0,
                cutoff,
                0,
                "profile-calendar",
                "a" * 64,
                tuple(
                    CoverageSlot(f"{i:03}", SECOND, "EARNINGS.RELEASE", ISSUER) for i in range(100)
                ),
            )
            # Stable lexical slot order is explicit, independent of construction order.
            from dataclasses import replace

            universe = replace(universe, slots=tuple(sorted(universe.slots, key=lambda s: s.key)))
            repo.append_universe(universe)
            started = perf_counter()
            coverage(universe, (), cutoff)
            metrics["coverage_calculation_seconds"] = perf_counter() - started
            started = perf_counter()
            results = tuple(engine.source_reliability(q) for q in queries)
            metrics["calculation_and_persistence_seconds"] = perf_counter() - started
            started = perf_counter()
            for result in results:
                validate(repo, result)
            metrics["compact_validation_seconds"] = perf_counter() - started
            started = perf_counter()
            for result in results:
                validate(repo, result, deep=True)
            metrics["deep_validation_seconds"] = perf_counter() - started
            started = perf_counter()
            for q in queries:
                engine.source_reliability(q)
            metrics["ordinary_query_seconds"] = perf_counter() - started
            serialized = tuple(dumps(result) for result in results)
            metrics["sample_counts"] = [r.sample_count for r in results]
            metrics["result_bytes"] = sum(len(v) for v in serialized)
            metrics["indexed_rows"] = store._rows("SELECT COUNT(*) FROM reliability_rows")[0][0]
            metrics["artifact_count"] = store._rows("SELECT COUNT(*) FROM reliability_artifacts")[
                0
            ][0]
        started = perf_counter()
        with SQLiteEventStore(path) as store:
            engine = ReliabilityQueries(ReliabilityRepository(store))
            assert tuple(dumps(engine.source_reliability(q)) for q in queries) == serialized
        metrics["restart_and_query_seconds"] = perf_counter() - started
        metrics["sqlite_bytes"] = path.stat().st_size
        metrics["restart_bytes_identical"] = True
    profiler.disable()
    stats = pstats.Stats(profiler)
    metrics["profile_total_seconds"] = stats.total_tt
    metrics["profile_calls"] = stats.total_calls
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = json.dumps(profile(args.count), indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(output)
    print(output, end="")
