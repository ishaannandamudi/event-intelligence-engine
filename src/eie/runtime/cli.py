"""Minimal foreground runtime controls; live networking requires an explicit flag."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from eie.config.settings import Settings
from eie.core.errors import EIEError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.ingestion.processors import AdapterProcessor
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.adapter import SecAdapter, SecIngestor
from eie.providers.sec.models import CIK
from eie.providers.sec.settings import SecSettings
from eie.runtime.clock import SystemClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.coordinator import IngestionCoordinator
from eie.runtime.providers import ProviderRegistration, bls_provider, sec_provider
from eie.runtime.shutdown import termination_signals
from eie.runtime.sources import AcquiredPayload, FixtureSource
from eie.transport.models import HttpResponse


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="eie")
    result.add_argument("--database", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-store")
    health = commands.add_parser("provider-health")
    health.add_argument("provider")
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("provider")
    checkpoint.add_argument("resource")
    for name in ("ingest-once", "run"):
        command = commands.add_parser(name)
        command.add_argument("provider", choices=("SEC_EDGAR", "BLS"))
        command.add_argument("--cik", default="123456")
        mode = command.add_mutually_exclusive_group(required=True)
        mode.add_argument("--live", action="store_true")
        mode.add_argument("--fixture", type=Path)
    return result


def registration(args: argparse.Namespace, clock: SystemClock) -> ProviderRegistration:
    if args.fixture is not None:
        with args.fixture.open("rb") as handle:
            payload = handle.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValidationError("fixture exceeds runtime payload bound")
        if args.provider == "BLS":
            live = bls_provider(clock)
            source = FixtureSource(
                "BLS", live.source.resource_id, (AcquiredPayload(HttpResponse(200, body=payload)),)
            )
            return ProviderRegistration(source, live.processor, enabled=True)
        cik = CIK.parse(args.cik)
        url = f"https://data.sec.gov/submissions/CIK{cik.digits}.json"
        source = FixtureSource(
            "SEC_EDGAR",
            f"submissions:{cik.digits}",
            (AcquiredPayload(HttpResponse(200, body=payload)),),
        )
        processor = AdapterProcessor(
            "sec",
            SecIngestor(Settings(sec=SecSettings(enabled=True))),
            SecAdapter(),
            source_url=url,
            expected_cik=cik,
            clock=clock,
        )
        return ProviderRegistration(source, processor, enabled=True)
    if args.provider == "BLS":
        return bls_provider(clock, enabled=True)
    return sec_provider(
        CIK.parse(args.cik), os.environ.get("EIE_SEC_USER_AGENT", ""), clock, enabled=True
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store: SQLiteEventStore | None = None
    close_store = True
    try:
        clock = SystemClock()
        configured = registration(args, clock) if args.command in ("ingest-once", "run") else None
        store = SQLiteEventStore(args.database)
        output: object
        if args.command == "validate-store":
            errors = store.validate()
            output = {
                "errors": errors,
                "counts": store.counts(),
                "schema_version": store.schema_version,
            }
            code = int(bool(errors))
        elif args.command == "checkpoint":
            output, code = store.checkpoint(args.provider, args.resource), 0
        elif args.command == "provider-health":
            history = store.operations(args.provider, "health")
            output, code = (history[-1] if history else None), 0
        else:
            if configured is None:
                raise ValidationError("runtime provider required")
            runtime = IngestionCoordinator(
                store, (configured,), RuntimeConfig(enabled=True), clock=clock
            )
            if args.command == "ingest-once":
                result = runtime.ingest_once(args.provider)
                output, code = result, int(not result.committed or runtime.fatal_error is not None)
            else:
                try:
                    with termination_signals(runtime.request_stop):
                        runtime.start()
                        while not runtime.wait_for_stop():
                            pass
                finally:
                    graceful = runtime.shutdown(close_store=False)
                    close_store = graceful
                output = {
                    "graceful": graceful,
                    "metrics": runtime.metrics.snapshot(runtime.queue.depth),
                }
                code = int(not graceful or runtime.fatal_error is not None)
        print(canonical_json_bytes(output).decode())
        return code
    except (EIEError, OSError, json.JSONDecodeError):
        print("runtime configuration or persistence failure", file=sys.stderr)
        return 2
    finally:
        if store is not None and close_store:
            store.close()
