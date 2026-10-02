"""Small historical controls with pure planning and bounded offline input files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import cast

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import CoverageClaim, JobStatus, TimeRange, cursor_value
from eie.backfill.planner import plan
from eie.backfill.rebuild import rebuild
from eie.backfill.sec import SecHistoryProcessor, sec_history
from eie.backfill.sources import (
    ContractHistoryProcessor,
    FixtureHistoricalSource,
    HistoricalPage,
    HistoricalProcessor,
    HistoricalSource,
)
from eie.core.errors import ValidationError
from eie.datasets.models import BackfillJob, DatasetDefinition
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.datasets.validation import validate_dataset
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.models import CIK
from eie.runtime.clock import SystemClock
from eie.runtime.shutdown import termination_signals


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    history = commands.add_parser("backfill").add_subparsers(dest="action", required=True)
    dry = history.add_parser("plan")
    dry.add_argument("--definition", required=True, type=Path)
    for name in ("run", "resume"):
        command = history.add_parser(name)
        if name == "run":
            command.add_argument("--definition", required=True, type=Path)
            command.add_argument("--build-version", default="1")
        else:
            command.add_argument("--build", required=True)
        command.add_argument("--archive", required=True, type=Path)
        command.add_argument("--max-pages", type=int)
        mode = command.add_mutually_exclusive_group(required=True)
        mode.add_argument("--pages", type=Path)
        mode.add_argument("--live", action="store_true")
    status = history.add_parser("status")
    status.add_argument("--build", required=True)
    dataset = commands.add_parser("dataset").add_subparsers(dest="action", required=True)
    for name in ("manifest", "validate", "rebuild", "events-as-of"):
        command = dataset.add_parser(name)
        command.add_argument("--build", required=True)
        if name in ("validate", "rebuild"):
            command.add_argument("--archive", required=True, type=Path)
        if name == "rebuild":
            command.add_argument("--build-version", required=True)
        if name == "events-as-of":
            command.add_argument("--at-ns", required=True, type=int)
        if name == "manifest":
            command.add_argument("--output", type=Path)


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        body = handle.read(20_000_001)
    if len(body) > 20_000_000:
        raise ValidationError("historical control file exceeds configured bound")
    return body


def definition(path: Path) -> DatasetDefinition:
    value = codec.loads(read_bounded(path))
    if not isinstance(value, DatasetDefinition):
        raise ValidationError("historical definition file requires a typed dataset definition")
    return value


def processor_for(spec_provider: str, job: BackfillJob) -> HistoricalProcessor:
    spec = next(s for s in job.definition.sources if s.provider_id == spec_provider)
    if spec.provider_id == "SEC_EDGAR":
        processor = SecHistoryProcessor(tuple(CIK.parse(r[4:]) for r, _ in spec.identity_bindings))
    else:
        # The CLI has one fixed audited-extract parser; arbitrary code versions require the API.
        return ContractHistoryProcessor()
    return processor


def registrations(
    args: argparse.Namespace, job: BackfillJob, clock: SystemClock
) -> tuple[tuple[HistoricalSource, HistoricalProcessor], ...]:
    values: object = [] if args.pages is None else codec.strict_json(read_bounded(args.pages))
    if not isinstance(values, list) or len(values) > 10_000:
        raise ValidationError("historical fixture requires a bounded page list")
    result: list[tuple[HistoricalSource, HistoricalProcessor]] = []
    for spec in job.definition.sources:
        processor = processor_for(spec.provider_id, job)
        if args.live:
            if spec.provider_id != "SEC_EDGAR":
                raise ValidationError("only SEC has an enabled historical live CLI path")
            source, native = sec_history(
                tuple(CIK.parse(r[4:]) for r in spec.resources),
                os.environ.get("EIE_SEC_USER_AGENT", ""),
                clock,
                issuer_ciks=tuple(CIK.parse(r[4:]) for r, _ in spec.identity_bindings),
            )
            result.append((source, native))
            continue
        pages: dict[tuple[str, int, int, str | None], HistoricalPage | Exception] = {}
        for row in values:
            if not isinstance(row, dict) or set(row) != {
                "provider",
                "resource",
                "start_ns",
                "end_ns",
                "cursor",
                "body_hex",
                "source_reference",
                "next_cursor",
                "coverage",
                "too_many_results",
            }:
                raise ValidationError("historical fixture page fields mismatch")
            row = cast(dict[str, object], row)
            if row["provider"] != spec.provider_id:
                continue
            resource, start, end, cursor = (
                row["resource"],
                row["start_ns"],
                row["end_ns"],
                row["cursor"],
            )
            reference, next_cursor, coverage = (
                row["source_reference"],
                row["next_cursor"],
                row["coverage"],
            )
            if (
                not isinstance(resource, str)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(reference, str)
                or not isinstance(coverage, str)
                or (cursor is not None and not isinstance(cursor, str))
                or (next_cursor is not None and not isinstance(next_cursor, str))
                or not isinstance(row["too_many_results"], bool)
            ):
                raise ValidationError("typed historical fixture fields required")
            TimeRange(start, end)
            if cursor is not None:
                cursor_value(cursor)
            encoded = row["body_hex"]
            if not isinstance(encoded, str) or len(encoded) > spec.capability.max_payload_bytes * 2:
                raise ValidationError("historical fixture page exceeds source bound")
            try:
                page = HistoricalPage(
                    bytes.fromhex(encoded),
                    reference,
                    next_cursor=next_cursor,
                    coverage=CoverageClaim(coverage),
                    too_many_results=row["too_many_results"],
                )
            except (ValueError, TypeError) as exc:
                raise ValidationError("invalid historical fixture page") from exc
            key = (resource, start, end, cursor)
            if key in pages or row["resource"] not in spec.resources:
                raise ValidationError("duplicate or unconfigured historical fixture resource")
            pages[key] = page
        result.append((FixtureHistoricalSource(spec, pages), processor))
    return tuple(result)


def execute(args: argparse.Namespace) -> tuple[object, int]:
    if args.command == "backfill" and args.action == "plan":
        job = BackfillJob(definition(args.definition))
        return {"job": job, "build_id": job.build_id, "partitions": plan(job)}, 0
    if args.action != "run" and not Path(args.database).is_file():
        raise ValidationError("historical status/query requires an existing database")
    with SQLiteEventStore(args.database) as store:
        repository = DatasetRepository(store)
        if args.command == "backfill" and args.action == "run":
            job = BackfillJob(definition(args.definition), args.build_version)
        else:
            stored = repository.job(args.build)
            if stored is None:
                raise ValidationError("unknown historical build")
            job = stored
        if args.command == "backfill":
            if args.action == "status":
                return {
                    "build_id": job.build_id,
                    "status": repository.status(job.build_id),
                    "partitions": tuple(
                        {
                            "id": p.partition_id,
                            "status": state.status,
                            "pages": state.pages,
                            "items": state.items_committed,
                        }
                        for p, state in repository.partitions(job.build_id)
                    ),
                }, 0
            with FileArchive(args.archive) as archive:
                runtime = BackfillCoordinator(
                    repository, archive, registrations(args, job, SystemClock())
                )
                with termination_signals(runtime.request_pause):
                    status = runtime.run(job, max_pages=args.max_pages)
                return {"build_id": job.build_id, "status": status}, int(
                    status in (JobStatus.FAILED, JobStatus.CANCELLED)
                )
        manifest = repository.manifest(job.build_id)
        if manifest is None:
            raise ValidationError("dataset command requires a finalized manifest")
        if args.action == "manifest":
            if args.output is not None:
                args.output.write_bytes(codec.dumps(manifest))
            return {"manifest": manifest, "content_fingerprint": manifest.content_fingerprint}, 0
        if args.action == "events-as-of":
            return DatasetReader(repository, job.build_id).events_known_at(args.at_ns), 0
        with FileArchive(args.archive) as archive:
            if args.action == "validate":
                result = validate_dataset(repository, archive, job.build_id)
                return result, int(bool(result.errors))
            processors = {
                s.provider_id: processor_for(s.provider_id, job) for s in job.definition.sources
            }
            rebuilt = rebuild(repository, archive, job.build_id, processors, args.build_version)
            return {
                "build_id": rebuilt.build_id,
                "manifest": repository.manifest(rebuilt.build_id),
            }, 0
