"""Real offline multi-family source/intelligence/study index and bounded retrieval profile."""

import argparse
import cProfile
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.analogues.distance import distance, scales
from eie.analogues.models import AnalogueQuery, AnalogueRunManifest
from eie.analogues.pool import collect, unique_rows
from eie.analogues.repository import AnalogueRepository
from eie.analogues.responses import distribution
from eie.analogues.retrieval import retrieve
from eie.analogues.validation import validate
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
from eie.events.taxonomy import PREFIX, EventFamily, EventType
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from examples.analogue_fixtures import fixture
from examples.counterfactual_impact import supplied_market
from examples.event_studies import DAY
from examples.historical_analogues import POLICY
from examples.historical_rebuild import fixture as source_fixture
from examples.historical_rebuild import item


def historical(root: Path, count: int):
    job, _, processor = source_fixture()
    source = replace(
        job.definition.sources[0],
        resources=("events",),
        capability=HistoricalCapability(max_range_ns=20 * DAY),
    )
    interval = TimeRange(DAY, 15 * DAY)
    job = BackfillJob(
        replace(
            job.definition,
            interval=interval,
            sources=(source,),
            partition_policy=PartitionPolicy(20 * DAY),
        )
    )
    values, ids = [], []
    families = tuple(EventFamily)
    for i in range(count):
        at = 2 * DAY + (i + 1) * (12 * DAY // (count + 1))
        v = item(at, "analogue-" + str(i))
        family = families[i % len(families)]
        e = replace(
            v.record,
            identity=EventIdentity(
                EventType(family, PREFIX.get(family, family.value) + ".SYNTHETIC"),
                EventScope(ScopeKind.COMPANY, ("issuer_fixture",)),
                "analogue-" + str(i),
            ),
            published_at_ns=at,
        )
        values.append(
            replace(v, record=e, fragment_hash=sha256(canonical_json_bytes(e)).hexdigest())
        )
        ids.append(e.event_id)
    supplied = FixtureHistoricalSource(
        source,
        {
            ("events", DAY, 15 * DAY, None): HistoricalPage(
                codec.dumps(tuple(values)),
                "https://example.invalid/analogue-history",
                coverage=CoverageClaim.EXHAUSTIVE,
            )
        },
    )
    store = SQLiteEventStore(root / "studies.sqlite")
    repo = DatasetRepository(store)
    with FileArchive(root / "archive") as archive:
        BackfillCoordinator(
            repo, archive, ((supplied, processor),), clock=ManualClock(20 * DAY)
        ).run(job)
    return store, DatasetReader(repo, job.build_id), tuple(ids)


def profile(root: Path, count: int) -> dict[str, object]:
    stages: dict[str, float] = {}
    start = perf_counter()
    history = historical(root, count)
    stages["historical_build"] = perf_counter() - start
    data = fixture(
        root, POLICY, historical=history, market_factory=supplied_market, stage_times=stages
    )
    repo = data.repository
    result_bytes = 0
    results = []
    try:
        for target in data.rows[-10:]:
            q = AnalogueQuery(
                target.row_id,
                target.event_id,
                target.revision,
                target.dataset_id,
                target.feature_cutoff_ns,
                target.available_ns,
                POLICY.policy_id,
                data.spec,
            )
            with repo.store.transaction():
                start = perf_counter()
                pool = collect(repo, q, POLICY)
                stages["indexed_blocked_retrieval"] = (
                    stages.get("indexed_blocked_retrieval", 0) + perf_counter() - start
                )
                _, eligible = unique_rows(pool, q, POLICY)
                start = perf_counter()
                prior_scales = scales(target, eligible, POLICY)
                stages["prior_scaling"] = stages.get("prior_scaling", 0) + perf_counter() - start
                by_row = {r.feature_row_id: r for r in pool.responses}
                start = perf_counter()
                measured = [
                    distance(target, row, by_row[row.row_id], POLICY, prior_scales)
                    for row in eligible
                ]
                stages["distance_calculation"] = (
                    stages.get("distance_calculation", 0) + perf_counter() - start
                )
                start = perf_counter()
                selected = tuple(
                    sorted(
                        (c for c, reason in measured if c is not None and reason is None),
                        key=lambda c: (c.total_distance, c.event_id, c.revision, c.row_id),
                    )[: q.top_k]
                )
                stages["top_k"] = stages.get("top_k", 0) + perf_counter() - start
                start = perf_counter()
                distribution(
                    q.response,
                    tuple((c.row_id, c.response_value) for c in selected),
                    POLICY.minimum_history,
                )
                stages["distribution_calculation"] = (
                    stages.get("distribution_calculation", 0) + perf_counter() - start
                )
                result = retrieve(pool, q, POLICY)
                start = perf_counter()
                repo._result(result)
                stages["result_persistence"] = (
                    stages.get("result_persistence", 0) + perf_counter() - start
                )
                results.append(result)
                result_bytes += len(codec.dumps(result))
        final = results[-1]
        response = repo.index_response(data.rows[-1].row_id, data.studies[-1].study_id, data.spec)
        start = perf_counter()
        reaction = repo.attach_reaction(
            final.result_id, response.response_id, final.query.available_ns + 1
        )
        stages["reaction_comparison_and_persistence"] = perf_counter() - start
        ordered = tuple(sorted(results, key=lambda r: r.result_id))
        run = AnalogueRunManifest(
            tuple(r.result_id for r in ordered),
            tuple(r.fingerprint for r in ordered),
            final.query.dataset_id,
            final.query.available_ns,
        )
        repo.append_run(run)
        start = perf_counter()
        assert validate(repo, final.result_id, deep=True).state.value != "INVALID"
        stages["deep_validation"] = perf_counter() - start
        before = codec.dumps(final), codec.dumps(reaction)
        counts = {
            table: repo.store._rows("SELECT COUNT(*) FROM " + table)[0][0]
            for table in (
                "analogue_features",
                "analogue_responses",
                "ei_results",
                "study_results",
                "analogue_results",
            )
        }
        plans = repo.store._rows(
            "EXPLAIN QUERY PLAN SELECT id FROM analogue_features "
            "WHERE dataset=? AND policy=? AND event_type=? AND event_available<=?",
            (
                final.query.dataset_id,
                final.query.policy_id,
                data.rows[-1].event_type,
                final.query.known_as_of_ns,
            ),
        )
    finally:
        data.store.close()
    start = perf_counter()
    with SQLiteEventStore(root / "studies.sqlite") as store:
        restarted = AnalogueRepository(store)
        assert codec.dumps(restarted.get(final.result_id)) == before[0]
        assert codec.dumps(restarted.reaction(reaction.reaction_id)) == before[1]
        assert restarted.run(run.run_id) == run
    stages["restart_query"] = perf_counter() - start
    return {
        "workload": count,
        "family_count": 10,
        "target_queries": len(results),
        "rows": counts,
        "stages_seconds": stages,
        "blocked_counts": [r.diagnostics.blocked_candidates for r in results],
        "selected_counts": [len(r.candidates) for r in results],
        "query_plan": plans,
        "result_bytes": result_bytes,
        "sqlite_bytes": (root / "studies.sqlite").stat().st_size,
        "restart_byte_identical": True,
        "ordinary_queries_recompute_historical_intelligence": False,
        "qualification": (
            "Representative real source/intelligence/study workload; no 100k-row latency claim. "
            "Staged distance/distribution calculations are repeated by checked result assembly."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 70 <= args.count <= 10000:
        parser.error("representative workload requires 70..10000 events")
    profiler = cProfile.Profile()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        value = profiler.runcall(profile, root, args.count)
    # Function totals expose the dominant work without machine-specific file paths.
    stats = sorted(profiler.getstats(), key=lambda entry: entry.totaltime, reverse=True)[:15]
    value["dominant_functions"] = [
        (
            entry.code.co_name if hasattr(entry.code, "co_name") else str(entry.code),
            entry.callcount,
            entry.totaltime,
        )
        for entry in stats
    ]
    output = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(output)
    print(output)
