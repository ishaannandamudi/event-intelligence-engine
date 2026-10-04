"""Measured local retained-artifact workloads; no service SLA or alpha claim."""

import cProfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.core.serialization import canonical_json_bytes
from eie.persistence.sqlite import SQLiteEventStore
from eie.service.api import ServiceApi
from eie.service.engine import EventIntelligenceService
from eie.service.repository import ServiceRepository
from eie.service.schema import ComponentKind, ValidationDepth
from eie.service.serialization import wire
from eie.service.validation import validate
from examples.final_snapshot import CATALOG, CUTOFF, VALID, build


def profile(root: Path) -> dict[str, object]:
    stages: dict[str, float] = {}

    def measured(name, function):
        start = perf_counter()
        value = function()
        stages[name] = perf_counter() - start
        return value

    scenario = measured("fixture_build", lambda: build(root))
    provider = scenario.service
    request = scenario.snapshot.request
    full = measured(
        "one_full_uncached", lambda: provider.snapshot(replace(request, known_as_of_ns=CUTOFF + 1))
    )
    compact_request = replace(request, components=(ComponentKind.EVENTS,))
    compact = measured("one_compact", lambda: provider.snapshot(compact_request))
    measured(
        "single_symbol",
        lambda: provider.snapshot_for_symbol(
            "EXM", CUTOFF, VALID, catalog_id=CATALOG, exchange="NYSE"
        ),
    )
    measured("historical", lambda: provider.snapshot(replace(request, known_as_of_ns=CUTOFF - 1)))
    measured("component_specific", lambda: provider.analogues(request))
    measured("persisted_lookup", lambda: provider.repository.snapshot(full.snapshot_id))
    measured("cache_validated", lambda: provider.snapshot(request))
    measured("batch_100_compact", lambda: provider.snapshots((compact_request,) * 100))
    measured("batch_500_compact", lambda: provider.snapshots((compact_request,) * 500))
    measured(
        "compact_validation", lambda: validate(provider.repository, scenario.snapshot.snapshot_id)
    )
    measured(
        "deep_validation",
        lambda: validate(provider.repository, scenario.snapshot.snapshot_id, ValidationDepth.DEEP),
    )
    payload = canonical_json_bytes(
        {
            "catalog_id": CATALOG,
            "scope": {"kind": "MARKET"},
            "known_as_of_ns": CUTOFF,
            "valid_at_ns": VALID,
        }
    )
    measured(
        "http_json_dispatch", lambda: ServiceApi(provider).dispatch("POST", "/snapshot", payload)
    )
    sizes = {
        "full_json_bytes": len(canonical_json_bytes(wire(full))),
        "compact_json_bytes": len(canonical_json_bytes(wire(compact))),
        "service_payload_bytes": provider.repository.store._rows(
            "SELECT SUM(bytes) FROM service_snapshots"
        )[0][0],
    }
    scenario.store.close()
    path = root / "studies.sqlite"

    def restarted():
        with SQLiteEventStore(path) as store:
            return EventIntelligenceService(ServiceRepository(store)).snapshot(request)

    measured("restart_and_query", restarted)
    sizes["sqlite_file_bytes"] = path.stat().st_size
    return {
        "seconds": stages,
        "storage": sizes,
        "batch_selection": "EVENTS; 500 full responses exceed default 20 MB bound",
        "http_measurement": "JSON dispatch, excluding TCP transport",
        "sla": "not asserted",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        profiler = cProfile.Profile()
        if args.profile:
            profiler.enable()
        result = profile(Path(directory))
        if args.profile:
            profiler.disable()
            profiler.dump_stats(args.profile)
        print(canonical_json_bytes(result).decode())
