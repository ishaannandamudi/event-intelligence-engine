"""Offline measured time/entity blocking over retained canonical history; no CI threshold."""

import argparse
import cProfile
import json
import pstats
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.graph.models import TraversalMode, TraversalPolicy
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.interactions.collection import collect
from eie.interactions.engine import InteractionEngine, calculate
from eie.interactions.models import DAY, InteractionPolicy, InteractionWindow, WindowOverride
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import OrderingBasis
from eie.interactions.validation import validate
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.entity_relationship_graph import fixture_nodes, ingest, relationship
from examples.interaction_fixtures import MINUTE, query, retained_event


def workload(root: Path, count: int) -> dict[str, object]:
    times = {}
    path = root / "profile.sqlite"
    policy = InteractionPolicy(
        window=InteractionWindow(
            overrides=(
                WindowOverride("MACRO", 10 * MINUTE, 10 * MINUTE, OrderingBasis.SCHEDULED_TIME),
            )
        )
    )
    nodes = fixture_nodes()
    a, b, c, *_ = nodes
    with SQLiteEventStore(path) as store:
        repo = InteractionRepository(store)
        graph = GraphRepository(store)
        for n in nodes:
            graph.append_node(n)
        ingest(
            graph,
            (relationship("ab", R.SUPPLIER_TO, a, b), relationship("bc", R.SUPPLIER_TO, b, c)),
            MINUTE,
        )
        start = perf_counter()
        roots = []
        for i in range(count):
            block = i // 100
            t = (block + 1) * DAY + (i % 100 + 1) * MINUTE
            entity = (
                a.node_id,
                b.node_id,
                c.node_id,
                "iss_profile_3",
                "iss_profile_4",
                "iss_profile_5",
                "iss_profile_6",
                "iss_profile_7",
                "iss_profile_8",
                "iss_profile_9",
            )[i % 10]
            event = retained_event(
                repo,
                f"event-{i}",
                t,
                time=t,
                entity=entity,
                bundle=f"bundle-{i // 2}",
                source_time=t - (i % 2) * MINUTE,
            )
            if i % 100 == 0:
                roots.append(event)
            if i % 100 == 50:
                retained_event(
                    repo,
                    f"macro-{block}",
                    t,
                    scheduled=t + 5 * MINUTE,
                    scope=ScopeKind.MACRO_GLOBAL,
                    family=EventFamily.MACRO,
                    code="MACRO.CPI.US",
                )
        times["source_and_index_seconds"] = perf_counter() - start
        final = roots[-1].revision.available_from_ns + 100 * MINUTE
        contexts = []
        blocked = []
        totals = dict.fromkeys(
            (
                "blocking",
                "pair_sequence_chronology",
                "persistence_snapshot",
                "query",
                "deep_validation",
            ),
            0.0,
        )
        for target in roots[-5:]:
            snap = traverse(
                graph,
                (a.node_id,),
                final,
                target.effective_at_ns,
                TraversalPolicy((R.SUPPLIER_TO,), max_depth=2, mode=TraversalMode.SIMPLE_PATHS),
            )
            graph.append_snapshot(snap)
            q = query(repo, target, final, policy, graph_id=snap.snapshot_id)
            with store.transaction():
                start = perf_counter()
                manifest = collect(repo, q, policy)
                totals["blocking"] += perf_counter() - start
                blocked.append(len(manifest.collision_row_ids))
                start = perf_counter()
                calculate(repo, manifest, policy)
                totals["pair_sequence_chronology"] += perf_counter() - start
            start = perf_counter()
            value = InteractionEngine(repo).interaction_context(q)
            totals["persistence_snapshot"] += perf_counter() - start
            contexts.append(value)
            start = perf_counter()
            repo.context(value.artifact_id)
            totals["query"] += perf_counter() - start
            start = perf_counter()
            validate(repo, value.artifact_id, deep=True)
            totals["deep_validation"] += perf_counter() - start
        times.update({k + "_seconds": v for k, v in totals.items()})
        size = sum(len(dumps(value)) for value in contexts)
        children = store._rows(
            "SELECT COALESCE(SUM(length(payload)),0) FROM interaction_artifacts"
        )[0][0]
        store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    start = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = InteractionRepository(store)
        for value in contexts:
            assert dumps(repo.context(value.artifact_id)) == dumps(value)
    times["restart_query_seconds"] = perf_counter() - start
    return {
        "events": count,
        "macro_events": count // 100,
        "issuer_blocks": 10,
        "queries": len(contexts),
        "blocked_rows_per_query": blocked,
        "collision_counts": [len(c.collision_ids) for c in contexts],
        "chronology_counts": [len(c.chronology_ids) for c in contexts],
        "context_bytes": size,
        "artifact_bytes": children,
        "sqlite_bytes": path.stat().st_size,
        "timings": times,
        "restart_bytes_identical": True,
        "qualification": "local supplied sources and two-hop graph; no CI threshold",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 100 <= args.count <= 5000:
        parser.error("count must be between 100 and 5000")
    profiler = cProfile.Profile()
    with TemporaryDirectory() as directory:
        result = profiler.runcall(workload, Path(directory), args.count)
    stats = pstats.Stats(profiler)
    result["profile_calls"] = stats.total_calls
    result["profile_total_seconds"] = stats.total_tt
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
