"""Final CLI commands share the typed provider boundary."""

from __future__ import annotations

import argparse
from pathlib import Path

from eie.core.errors import ValidationError
from eie.persistence.codec import strict_json
from eie.persistence.sqlite import SQLiteEventStore
from eie.service.api import ServiceApi, request
from eie.service.engine import EventIntelligenceService
from eie.service.http import HttpSettings, serve
from eie.service.recovery import backup, restore
from eie.service.repository import ServiceRepository
from eie.service.schema import ValidationDepth
from eie.service.serialization import wire
from eie.service.validation import validate

COMMANDS = frozenset(
    {
        "snapshot",
        "snapshots",
        "health",
        "ready",
        "validate",
        "backup",
        "restore",
        "version",
        "serve",
    }
)


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for name in ("snapshot", "snapshots"):
        commands.add_parser(name).add_argument("request", type=Path)
    for name in ("health", "ready", "version"):
        commands.add_parser(name)
    validation = commands.add_parser("validate")
    validation.add_argument("snapshot_id")
    validation.add_argument("--deep", action="store_true")
    commands.add_parser("backup").add_argument("output", type=Path)
    recovery = commands.add_parser("restore")
    recovery.add_argument("backup", type=Path)
    recovery.add_argument("output", type=Path)
    server = commands.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)


def execute(args: argparse.Namespace, store: SQLiteEventStore) -> tuple[object, int]:
    engine = EventIntelligenceService(ServiceRepository(store))
    if args.command == "version":
        return dict(engine.version()), 0
    if args.command == "health":
        return engine.health(), 0
    if args.command == "ready":
        result = engine.ready()
        return result, int(not result.historical_queries)
    if args.command == "validate":
        validation_result = validate(
            engine.repository,
            args.snapshot_id,
            ValidationDepth.DEEP if args.deep else ValidationDepth.COMPACT,
        )
        return validation_result, int(not validation_result.valid)
    if args.command == "backup":
        return backup(store, args.output), 0
    if args.command == "restore":
        return restore(args.backup, args.output), 0
    if args.command == "serve":
        serve(ServiceApi(engine), HttpSettings(args.host, args.port))
        return {"stopped": True}, 0
    with args.request.open("rb") as stream:
        body = stream.read(engine.limits.max_request_bytes + 1)
    if len(body) > engine.limits.max_request_bytes:
        raise ValidationError("CLI request bound exceeded")
    if args.command == "snapshot":
        return wire(engine.snapshot(request(strict_json(body)))), 0
    status, response = ServiceApi(engine).dispatch("POST", "/snapshots", body)
    return strict_json(response), int(status != 200)
