"""Strict bounded JSON dispatch; paths are fixed routes, never file locations."""

from dataclasses import fields

from eie.core.errors import EIEError, PersistenceError
from eie.core.fingerprints import fingerprint
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.events.scope import EventScope, ScopeKind
from eie.persistence.codec import strict_json
from eie.service import API_VERSION, SNAPSHOT_SCHEMA_VERSION
from eie.service.engine import EventIntelligenceService
from eie.service.errors import ServiceError
from eie.service.models import (
    ALL_COMPONENTS,
    BatchItem,
    BatchResult,
    ReadinessResult,
    ServiceFailure,
    SnapshotLimits,
    SnapshotRequest,
)
from eie.service.schema import ComponentKind, ServiceCode
from eie.service.serialization import wire
from eie.version import __version__


def mapping(
    value: object, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
        or any(not isinstance(k, str) for k in value)
    ):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    return value


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    return value


def integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    return value


def optional_ns(value: object) -> int | None:
    return None if value is None else require_ns(integer(value))


def components(value: object) -> tuple[ComponentKind, ...]:
    if not isinstance(value, list) or len(value) > 19 or any(not isinstance(v, str) for v in value):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    selected = tuple(ComponentKind(v) for v in value)
    if len(set(selected)) != len(selected):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    return tuple(sorted(selected))


def strings(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 128
        or any(not isinstance(v, str) for v in value)
    ):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    if len(value) != len(set(value)):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    return tuple(sorted(value))


def request(value: object) -> SnapshotRequest:
    data = mapping(
        value,
        {"catalog_id", "scope", "known_as_of_ns"},
        {
            "valid_at_ns",
            "exchange",
            "components",
            "required",
            "policy_ids",
            "build_ids",
            "limits",
            "schema_version",
        },
    )
    scope = mapping(data["scope"], {"kind"}, {"identifiers"})
    identifiers = scope.get("identifiers", [])
    if (
        not isinstance(identifiers, list)
        or len(identifiers) > 1
        or any(not isinstance(i, str) for i in identifiers)
    ):
        raise ServiceError(ServiceCode.INVALID_REQUEST)
    limits_data = mapping(data.get("limits", {}), set(), {f.name for f in fields(SnapshotLimits)})
    # Dataclass constructors enforce primitives and all temporal/size contracts.
    return SnapshotRequest(
        string(data["catalog_id"]),
        EventScope(ScopeKind(string(scope["kind"])), tuple(identifiers)),
        require_ns(integer(data["known_as_of_ns"])),
        optional_ns(data.get("valid_at_ns")),
        None if data.get("exchange") is None else string(data["exchange"]),
        ALL_COMPONENTS if "components" not in data else components(data["components"]),
        components(data.get("required", [])),
        strings(data.get("policy_ids", [])),
        strings(data.get("build_ids", [])),
        SnapshotLimits(**{k: integer(v) for k, v in limits_data.items()}),
        string(data.get("schema_version", SNAPSHOT_SCHEMA_VERSION)),
    )


class ServiceApi:
    def __init__(self, provider: EventIntelligenceService) -> None:
        self.provider = provider

    def response(self, value: object) -> bytes:
        return canonical_json_bytes(
            {
                "engine_version": __version__,
                "api_version": API_VERSION,
                "snapshot_schema": SNAPSHOT_SCHEMA_VERSION,
                "component_versions": dict(self.provider.version()),
                "value": wire(value),
            }
        )

    def batch(self, values: list[object]) -> BatchResult:
        requests = []
        items: dict[int, BatchItem] = {}
        positions = []
        for position, raw in enumerate(values):
            try:
                requests.append(request(raw))
                positions.append(position)
            except (EIEError, ValueError, TypeError, KeyError):
                items[position] = BatchItem(
                    position,
                    fingerprint("eie:service:invalid-request:v1", raw),
                    failure=ServiceFailure(ServiceCode.INVALID_REQUEST),
                )
        with self.provider.repository.store.transaction():
            outcomes = self.provider.snapshots(tuple(requests))
            for position, item in zip(positions, outcomes.items, strict=True):
                items[position] = BatchItem(
                    position, item.request_fingerprint, item.snapshot, item.failure
                )
            result = BatchResult(tuple(items[i] for i in range(len(values))))
            if len(self.response(result)) > self.provider.limits.max_batch_bytes:
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
            return result

    def dispatch(
        self, method: str, path: str, body: bytes = b"", *, request_id: str | None = None
    ) -> tuple[int, bytes]:
        try:
            if len(body) > self.provider.limits.max_request_bytes:
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
            value: object
            if method == "GET" and not body:
                if path == "/version":
                    value = dict(self.provider.version())
                elif path == "/health":
                    value = self.provider.health()
                elif path == "/ready":
                    value = self.provider.ready()
                elif path == "/metrics":
                    value = self.provider.metrics.snapshot()
                else:
                    raise ServiceError(ServiceCode.NOT_FOUND)
            elif method == "POST":
                try:
                    data = strict_json(body)
                except PersistenceError as exc:
                    raise ServiceError(ServiceCode.INVALID_REQUEST) from exc
                canonical_json_bytes(data)
                if path == "/snapshot":
                    value = self.provider.snapshot(request(data), request_id=request_id)
                elif path == "/snapshots":
                    batch = mapping(data, {"requests"})["requests"]
                    if not isinstance(batch, list) or len(batch) > self.provider.limits.max_batch:
                        raise ServiceError(ServiceCode.BOUND_EXCEEDED)
                    value = self.batch(batch)
                elif path in (
                    "/events",
                    "/upcoming",
                    "/impact",
                    "/analogues",
                    "/relationships",
                    "/reliability",
                ):
                    queries = {
                        "/events": self.provider.events,
                        "/upcoming": self.provider.upcoming,
                        "/impact": self.provider.impact,
                        "/analogues": self.provider.analogues,
                        "/relationships": self.provider.relationships,
                        "/reliability": self.provider.reliability,
                    }
                    value = queries[path](request(data))
                elif path == "/expand":
                    expand = mapping(data, {"snapshot_id", "owner_id"})
                    if any(not isinstance(v, str) or len(v) > 256 for v in expand.values()):
                        raise ServiceError(ServiceCode.INVALID_REQUEST)
                    value = self.provider.expand(
                        string(expand["snapshot_id"]), string(expand["owner_id"])
                    )
                else:
                    raise ServiceError(ServiceCode.NOT_FOUND)
            else:
                raise ServiceError(ServiceCode.INVALID_REQUEST)
            output = self.response(value)
            maximum = (
                self.provider.limits.max_batch_bytes
                if path == "/snapshots"
                else self.provider.limits.max_expansion_bytes
                if path == "/expand"
                else self.provider.limits.max_snapshot_bytes + 10000
            )
            if len(output) > maximum:
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
            return (
                503 if isinstance(value, ReadinessResult) and not value.historical_queries else 200
            ), output
        except (EIEError, ValueError, TypeError, KeyError, RecursionError) as exc:
            code = (
                exc.code
                if isinstance(exc, ServiceError)
                else ServiceCode.INTERNAL_VALIDATION_FAILURE
                if isinstance(exc, PersistenceError)
                else ServiceCode.INVALID_REQUEST
            )
            status = (
                404
                if code is ServiceCode.NOT_FOUND
                else 500
                if code is ServiceCode.INTERNAL_VALIDATION_FAILURE
                else 503
                if code is ServiceCode.NOT_READY
                else 400
            )
            return status, self.response(ServiceFailure(code))
