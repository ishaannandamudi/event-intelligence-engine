"""Transactional provider-neutral service over published immutable owners."""

import sqlite3
from dataclasses import fields, replace
from time import perf_counter_ns

from eie.core.errors import EIEError, PersistenceError
from eie.core.serialization import canonical_json_bytes
from eie.events.scope import EventScope, ScopeKind
from eie.service import owners
from eie.service.assembly import assemble
from eie.service.compatibility import versions
from eie.service.errors import ServiceError
from eie.service.health import health
from eie.service.models import (
    DEFAULT_LIMITS,
    BatchItem,
    BatchResult,
    EIESnapshot,
    EventSummary,
    HealthResult,
    ReadinessResult,
    ServiceFailure,
    SnapshotComponent,
    SnapshotLimits,
    SnapshotRequest,
)
from eie.service.observability import OperationalMetrics
from eie.service.readiness import readiness
from eie.service.repository import ServiceRepository
from eie.service.schema import ComponentKind, ServiceCode
from eie.service.serialization import wire
from eie.service.validation import validate


class EventIntelligenceService:
    def __init__(
        self,
        repository: ServiceRepository,
        *,
        limits: SnapshotLimits = DEFAULT_LIMITS,
        metrics: OperationalMetrics | None = None,
    ) -> None:
        self.repository, self.limits, self.metrics = (
            repository,
            limits,
            metrics or OperationalMetrics(),
        )

    def snapshot(self, request: SnapshotRequest, *, request_id: str | None = None) -> EIESnapshot:
        start = perf_counter_ns()
        try:
            if not isinstance(request, SnapshotRequest):
                raise ServiceError(ServiceCode.INVALID_REQUEST)
            if any(
                getattr(request.limits, f.name) > getattr(self.limits, f.name)
                for f in fields(SnapshotLimits)
            ):
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
            if request.valid_at_ns is None:
                request = replace(request, valid_at_ns=request.known_as_of_ns)
            with self.repository.store.transaction():
                cached = self.repository.cached(request)
                if cached is not None:
                    self.metrics.record("cache_hit", "OK")
                    if not validate(self.repository, cached.snapshot_id).valid:
                        raise ServiceError(ServiceCode.INTERNAL_VALIDATION_FAILURE)
                    value = cached
                else:
                    self.metrics.record("cache_miss", "OK")
                    before = perf_counter_ns()
                    value = assemble(self.repository, request)
                    self.metrics.record("assembly", "OK", perf_counter_ns() - before)
                    if len(canonical_json_bytes(wire(value))) > request.limits.max_snapshot_bytes:
                        raise ServiceError(ServiceCode.BOUND_EXCEEDED)
                    self.repository.append(value)
            self.metrics.record(
                "snapshot",
                "OK",
                perf_counter_ns() - start,
                request_id=request_id,
                snapshot_id=value.snapshot_id,
            )
            return value
        except EIEError as exc:
            code = (
                exc.code
                if isinstance(exc, ServiceError)
                else ServiceCode.INTERNAL_VALIDATION_FAILURE
                if isinstance(exc, PersistenceError)
                else ServiceCode.INVALID_REQUEST
            )
            if isinstance(exc.__cause__, sqlite3.OperationalError) and getattr(
                exc.__cause__, "sqlite_errorcode", None
            ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                self.metrics.record("db_lock", code.value)
            self.metrics.record(
                "snapshot", code.value, perf_counter_ns() - start, request_id=request_id
            )
            raise ServiceError(code) from exc

    def snapshot_for_symbol(
        self,
        symbol: str,
        known_as_of_ns: int,
        valid_at_ns: int | None = None,
        *,
        catalog_id: str,
        exchange: str,
    ) -> EIESnapshot:
        return self.snapshot(
            SnapshotRequest(
                catalog_id,
                EventScope(ScopeKind.SYMBOL, (symbol,)),
                known_as_of_ns,
                valid_at_ns,
                exchange,
            )
        )

    def snapshots(self, requests: tuple[SnapshotRequest, ...]) -> BatchResult:
        if (
            not isinstance(requests, tuple)
            or len(requests) > self.limits.max_batch
            or any(not isinstance(r, SnapshotRequest) for r in requests)
        ):
            raise ServiceError(ServiceCode.BOUND_EXCEEDED)
        items = []
        size = 0
        with self.repository.store.transaction():
            for position, request in enumerate(requests):
                try:
                    snapshot = self.snapshot(request)
                    item = BatchItem(position, request.fingerprint, snapshot=snapshot)
                except ServiceError as exc:
                    item = BatchItem(
                        position, request.fingerprint, failure=ServiceFailure(exc.code)
                    )
                items.append(item)
                size += len(canonical_json_bytes(wire(item)))
                if size > self.limits.max_batch_bytes:
                    raise ServiceError(ServiceCode.BOUND_EXCEEDED)
            result = BatchResult(tuple(items))
            if len(canonical_json_bytes(wire(result))) > self.limits.max_batch_bytes:
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
        self.metrics.record("batch", "OK")
        return result

    def expand(self, snapshot_id: str, owner_id: str) -> owners.Detail:
        with self.repository.store.transaction():
            snapshot = self.repository.snapshot(snapshot_id)
            if snapshot is None:
                raise ServiceError(ServiceCode.NOT_FOUND)
            reference = next(
                (r for c in snapshot.components for r in c.references if r.owner_id == owner_id),
                None,
            )
            if reference is None:
                raise ServiceError(ServiceCode.NOT_FOUND)
            p = next(
                self.repository.publication(i)
                for c in snapshot.components
                for i in c.publication_ids
                if self.repository.publication(i).reference == reference
            )
            if p.reference.available_ns > snapshot.request.known_as_of_ns:
                raise ServiceError(ServiceCode.INTERNAL_VALIDATION_FAILURE)
            value = owners.read(self.repository.store, reference.kind, reference.lookup)
            if len(canonical_json_bytes(value)) > min(
                snapshot.request.limits.max_expansion_bytes, self.limits.max_expansion_bytes
            ):
                raise ServiceError(ServiceCode.BOUND_EXCEEDED)
        self.metrics.record("expansion", "OK")
        return value

    def component(self, request: SnapshotRequest, kind: ComponentKind) -> SnapshotComponent:
        return self.snapshot(
            replace(
                request,
                components=(kind,),
                required=tuple(k for k in request.required if k is kind),
            )
        ).components[0]

    def events(self, request: SnapshotRequest) -> SnapshotComponent:
        return self.component(request, ComponentKind.EVENTS)

    def upcoming(self, request: SnapshotRequest) -> tuple[EventSummary, ...]:
        return tuple(
            r.event
            for r in self.events(request).references
            if r.event is not None
            and r.event.scheduled_for_ns is not None
            and r.event.scheduled_for_ns >= request.valid_ns
        )

    def impact(self, request: SnapshotRequest) -> SnapshotComponent:
        return self.component(request, ComponentKind.IMPACT)

    def analogues(self, request: SnapshotRequest) -> SnapshotComponent:
        return self.component(request, ComponentKind.ANALOGUES)

    def relationships(self, request: SnapshotRequest) -> SnapshotComponent:
        return self.component(request, ComponentKind.GRAPH)

    def reliability(self, request: SnapshotRequest) -> SnapshotComponent:
        return self.component(request, ComponentKind.SOURCE_RELIABILITY)

    def health(self) -> HealthResult:
        return health()

    def ready(
        self, *, catalogs: tuple[str, ...] = (), live_ingestion: bool | None = None
    ) -> ReadinessResult:
        result = readiness(self.repository, catalogs=catalogs, live_ingestion=live_ingestion)
        self.metrics.record("readiness", "OK" if result.historical_queries else "NOT_READY")
        return result

    def version(self) -> tuple[tuple[str, str], ...]:
        return versions()
