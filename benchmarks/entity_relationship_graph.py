"""Representative offline indexed graph profile; no CI timing threshold."""

import argparse
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.entities.models import Issuer
from eie.entities.sec import sec_issuer_id
from eie.graph.claims import ClaimRevisionKind
from eie.graph.exposure import ExposureMeasurement, ExposureType
from eie.graph.models import TraversalMode, TraversalPolicy
from eie.graph.nodes import foundation_node
from eie.graph.propagation import propagate
from eie.graph.queries import GraphQueries
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import Direction
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.traversal import traverse
from eie.graph.validation import validate_propagation, validate_snapshot
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.models import CIK
from examples.entity_relationship_graph import DATASET, SOURCE, event, ingest, relationship


def profile(root: Path, count: int = 2000) -> dict[str, object]:
    if not 20 <= count <= 3000:
        raise ValueError("representative profile supports 20..3000 nodes")
    nodes = tuple(
        foundation_node(
            Issuer(
                sec_issuer_id(CIK(f"{i + 10000:010d}")),
                f"Synthetic issuer {i}",
                f"{i + 10000:010d}",
            ),
            SOURCE,
            DATASET,
            0,
        )
        for i in range(count)
    )
    supplied = tuple(
        relationship(f"supply-{i:05d}-{jump}", R.SUPPLIER_TO, nodes[i], nodes[(i + jump) % count])
        for i in range(count)
        for jump in (1, 5, 10)
    )
    ownership = tuple(
        relationship(
            f"ownership-{i:05d}",
            R.OWNS_EQUITY_IN,
            nodes[i],
            nodes[i + 1],
            exposure=ExposureMeasurement(
                ExposureType.OWNERSHIP_PERCENT,
                10,
                "synthetic-owner",
                point=0.8 if i % 2 == 0 else 0.5,
            ),
        )
        for i in range(min(200, count - 1))
    )
    sample_count = min(100, count)
    with SQLiteEventStore(root / "graph.sqlite") as store:
        repo = GraphRepository(store)
        started = perf_counter()
        with store.transaction():
            for n in nodes:
                repo.append_node(n)
        node_seconds = perf_counter() - started
        started = perf_counter()
        first = ingest(repo, (*supplied, *ownership), 100)
        revision_rows = tuple(
            replace(
                i,
                exposure=ExposureMeasurement(
                    ExposureType.REVENUE_SHARE, 150, "synthetic-revision", point=0.1
                ),
                revision_kind=ClaimRevisionKind.UPDATE,
            )
            for i in supplied[: min(300, count)]
        )
        second = ingest(repo, revision_rows, 200)
        ingestion_seconds = perf_counter() - started
        started = perf_counter()
        q = GraphQueries(repo)
        adjacent = tuple(
            q.neighbors(n.node_id, 250, 250, relationship_types=(R.SUPPLIER_TO,))
            for n in nodes[:sample_count]
        )
        adjacency_seconds = perf_counter() - started
        spec = TraversalPolicy((R.SUPPLIER_TO,), max_depth=2)
        started = perf_counter()
        snapshots = tuple(
            traverse(repo, (n.node_id,), 250, 250, spec) for n in nodes[:sample_count]
        )
        traversal_seconds = perf_counter() - started
        exposure_spec = TraversalPolicy(
            (R.OWNS_EQUITY_IN,),
            max_depth=2,
            direction=Direction.OUTGOING,
            mode=TraversalMode.SIMPLE_PATHS,
            derive_ownership=True,
        )
        started = perf_counter()
        exposure = traverse(repo, (nodes[0].node_id,), 250, 250, exposure_spec)
        exposure_seconds = perf_counter() - started
        assert any(
            p.depth == 2 and p.derived_exposure is not None and p.derived_exposure.point == 0.4
            for p in exposure.paths
        )
        started = perf_counter()
        candidates = tuple(
            propagate(event(n.node_id), repo, 250, 250, spec) for n in nodes[: min(20, count)]
        )
        propagation_seconds = perf_counter() - started
        started = perf_counter()
        for snapshot in snapshots[:20]:
            assert validate_snapshot(snapshot, repo).valid
        for value in candidates:
            assert validate_propagation(value, repo).valid
        validation_seconds = perf_counter() - started
        started = perf_counter()
        for value in candidates:
            repo.append_propagation(value)
        repo.append_snapshot(exposure)
        persistence_seconds = perf_counter() - started
        snapshot_bytes = codec.dumps(exposure)
        ids = tuple(p.result_id for p in candidates)
        assert repo.last_batch(SOURCE.name, "relationships") == second
        size = sum(p.stat().st_size for p in root.glob("graph.sqlite*"))
    started = perf_counter()
    with SQLiteEventStore(root / "graph.sqlite") as restarted:
        repo = GraphRepository(restarted)
        assert codec.dumps(repo.snapshot(exposure.snapshot_id)) == snapshot_bytes
        assert tuple(repo.propagation(i) for i in ids) == candidates
        assert repo.batch(first.batch_id) == first and repo.batch(second.batch_id) == second
        assert not restarted._rows("PRAGMA foreign_key_check")
    restart_seconds = perf_counter() - started
    return {
        "nodes": count,
        "semantic_edges": len(supplied) + len(ownership),
        "claim_revisions": len(supplied) + len(ownership) + len(revision_rows),
        "source_items": 2,
        "node_ingestion_seconds": node_seconds,
        "atomic_source_ingestion_and_storage_seconds": ingestion_seconds,
        "adjacency_samples": sample_count,
        "point_in_time_adjacency_seconds": adjacency_seconds,
        "mean_adjacency_seconds": adjacency_seconds / sample_count,
        "adjacent_edges_returned": sum(len(v) for v in adjacent),
        "depth_2_traversal_samples": sample_count,
        "depth_2_traversal_seconds": traversal_seconds,
        "ownership_path_seconds": exposure_seconds,
        "propagation_samples": len(candidates),
        "propagation_seconds": propagation_seconds,
        "validation_seconds": validation_seconds,
        "artifact_persistence_seconds": persistence_seconds,
        "restart_and_verified_queries_seconds": restart_seconds,
        "database_bytes_including_wal_at_sample": size,
        "all_reloaded_fingerprints_verified": True,
        "workload": (
            "deterministic synthetic generic issuers, cyclic supply network, sourced ownership "
            "chain; bounded local queries; no CI threshold"
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=2000)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        print(json.dumps(profile(Path(directory), args.nodes), indent=2, sort_keys=True))
