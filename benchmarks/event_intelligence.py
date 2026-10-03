"""Offline retained-history profile; real SQLite joins, no CI timing threshold."""

import argparse
import cProfile
import json
import pstats
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.core.fingerprints import fingerprint
from eie.corporate.types import CorporateStatus, EarningsPeriod
from eie.events.taxonomy import PREFIX, EventFamily, EventType
from eie.evidence.claims import NumericRange, StatusValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.repository import EvidenceRepository
from eie.expectations.units import Quantity
from eie.intelligence.collection import collect
from eie.intelligence.engine import calculate
from eie.intelligence.materiality import calculate_materiality
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.novelty import calculate_novelty
from eie.intelligence.policy import IntelligenceLimits, IntelligencePolicy
from eie.intelligence.queries import IntelligenceQueries
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.uncertainty import decompose
from eie.intelligence.validation import validate
from eie.nlp.engine import TextEngine
from eie.nlp.repository import TextRepository
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import DATASET, ISSUER, USD, batch, source
from examples.text_demo import BODY, CATALOG, item
from examples.text_demo import source as text_source


def profile(root: Path, events: int = 10000) -> dict[str, object]:
    if not 100 <= events <= 10000:
        raise ValueError("100 to 10000 retained events required")
    path = root / "intelligence-profile.sqlite"
    p = source("PROFILE", family="synthetic-primary")
    families = tuple(EventFamily)
    selected = []
    stages = dict.fromkeys(
        ("prior_query", "novelty", "materiality", "uncertainty", "assembly", "persistence"), 0.0
    )
    start = perf_counter()
    with SQLiteEventStore(path) as store:
        evidence = EvidenceRepository(store)
        engine = EvidenceEngine(evidence)
        with store.transaction():
            for n in range(events):
                family = families[n % len(families)]
                entity = (
                    ISSUER
                    if n % 100 == 0
                    else "iss_" + fingerprint("eie:profile-entity:v1", n % 100)
                )
                quarter_index = n // 100
                year, quarter = 2000 + quarter_index // 4, quarter_index % 4 + 1
                period = EarningsPeriod(year, quarter, f"{year}-{quarter * 3:02d}-28")
                value = Quantity((100 + quarter_index * 2 + n % 7) * 1_000_000, USD)
                measurement = "EARNINGS.REVENUE"
                if family is EventFamily.CORPORATE_ACTION:
                    measurement = "GUIDANCE.REVENUE"
                    period = EarningsPeriod(2026, 4, "2026-12-31")
                if n % 17 == 0:
                    value = NumericRange(value, Quantity(value.value * 1.1, USD))
                if family in (EventFamily.MANAGEMENT_GOVERNANCE, EventFamily.OTHER):
                    value = StatusValue(CorporateStatus.PENDING)
                b = batch(
                    p,
                    n + 100,
                    value,
                    key=f"history-{n:05d}",
                    period=period,
                    measurement=measurement,
                    subject_id=entity,
                    event_type=EventType(family, PREFIX.get(family, family.value) + ".PROFILE"),
                )
                engine.ingest(p, b)
                if n % (events // 10) == (n // (events // 10)) % len(families):
                    selected.append(b.events[0])
        ingest_s = perf_counter() - start
        tp = text_source("PROFILE_TEXT")
        tp = replace(tp, evidence=replace(tp.evidence, dataset=DATASET))
        doc, payload = item(tp, BODY, events + 1000)
        run = TextEngine(TextRepository(store)).ingest(tp, doc, payload, CATALOG, events + 2000)
        selected.extend(store.get_event(a.event_id, run.available_ns) for a in run.accepted)
        repo = IntelligenceRepository(store)
        policy, limits = IntelligencePolicy(), IntelligenceLimits()
        repo.append_policy(policy)
        results = []
        profiler = cProfile.Profile()
        profiler.enable()
        for event in selected:
            request = IntelligenceRequest(
                event.event_id,
                event.revision.number,
                DATASET.dataset_id,
                event.revision.available_from_ns,
                event.revision.available_from_ns,
                policy.policy_id,
            )
            with store.transaction():
                clock = perf_counter()
                data = collect(store, request, policy, limits)
                stages["prior_query"] += perf_counter() - clock
                clock = perf_counter()
                novelty = calculate_novelty(data, policy)
                stages["novelty"] += perf_counter() - clock
                clock = perf_counter()
                materiality = calculate_materiality(data, novelty, policy)
                stages["materiality"] += perf_counter() - clock
                clock = perf_counter()
                uncertainty = decompose(data, materiality)
                stages["uncertainty"] += perf_counter() - clock
                clock = perf_counter()
                result = calculate(data, policy, limits)
                assert (result.novelty, result.materiality, result.uncertainty) == (
                    novelty,
                    materiality,
                    uncertainty,
                )
                stages["assembly"] += perf_counter() - clock
                clock = perf_counter()
                repo.persist_resolutions(data, limits)
                repo._append(result, data)
                evidence.seal(DATASET.dataset_id, request.known_as_of_ns)
                stages["persistence"] += perf_counter() - clock
                results.append(result)
            print(f"profiled {len(results)}/{len(selected)} results", flush=True)
        profiler.disable()
        stats = pstats.Stats(profiler)
        dominant = sorted(
            (
                (key[0].split("/eie/")[-1] + ":" + str(key[1]) + ":" + key[2], value[3])
                for key, value in stats.stats.items()
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:15]
        clock = perf_counter()
        q = IntelligenceQueries(repo, DATASET.dataset_id)
        assert len(q.history(events + 3000)) == len(results)
        q.history(events + 3000, measurement="EARNINGS.REVENUE")
        query_s = perf_counter() - clock
        saved = tuple((r.intelligence_id, dumps(r)) for r in results)
        counts = {
            t: store._rows(f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in (
                "events",
                "ce_claims",
                "ce_assertions",
                "te_documents",
                "ei_inputs",
                "ei_results",
                "ei_measurements",
            )
        }
    clock = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = IntelligenceRepository(store)
        assert tuple(dumps(repo.get(i)) for i, _ in saved) == tuple(b for _, b in saved)
        for identity, _ in (saved[0], saved[-1]):
            assert validate(repo, identity).state.value != "INVALID"
    return {
        "historical_events": events,
        "historical_families": len(families),
        "intelligence_results": len(results),
        "ingestion_seconds": ingest_s,
        "stage_seconds": stages,
        "history_query_seconds": query_s,
        "restart_and_two_recalculations_seconds": perf_counter() - clock,
        "database_bytes": path.stat().st_size,
        "result_bytes": sum(len(b) for _, b in saved),
        "table_counts": counts,
        "dominant_cumulative_seconds": dominant,
        "restart_byte_identical": True,
        "measurement_note": (
            "Assembly recalculates components to check staged equality; "
            "profiling overhead is included."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as folder:
        result = profile(Path(folder), args.events)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output)
