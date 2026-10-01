"""Append-only, source-specific schedule coverage assertions."""

from dataclasses import dataclass

from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.events.scope import ApplicabilityQuery, EventScope, applies_directly
from eie.scheduled.models import CoverageStatus, CoverageSummary
from eie.sources.models import SourceHealth, SourceHealthState, SourceIdentity


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """One source's reported health for a known event-time interval."""

    source: SourceIdentity
    start_ns: int
    end_ns: int
    available_from_ns: int
    health: SourceHealth
    scope: EventScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceIdentity) or not isinstance(self.health, SourceHealth):
            raise ValidationError("typed source and health required")
        if self.scope is not None and not isinstance(self.scope, EventScope):
            raise ValidationError("typed coverage scope required")
        for name in ("start_ns", "end_ns", "available_from_ns"):
            require_ns(getattr(self, name), name)
        if self.end_ns <= self.start_ns:
            raise TemporalConsistencyError("coverage interval must be positive")
        for stamp in (self.health.last_success_ns, self.health.last_attempt_ns):
            if stamp is not None and stamp > self.available_from_ns:
                raise TemporalConsistencyError("source health follows coverage availability")


class CoverageLedger:
    def __init__(self) -> None:
        self._records: list[CoverageRecord] = []

    def append(self, record: CoverageRecord) -> bool:
        for previous in self._records:
            key = (
                record.source.name,
                record.start_ns,
                record.end_ns,
                record.available_from_ns,
                record.scope,
            )
            previous_key = (
                previous.source.name,
                previous.start_ns,
                previous.end_ns,
                previous.available_from_ns,
                previous.scope,
            )
            if key == previous_key:
                if record != previous:
                    raise PersistenceError("conflicting coverage assertion at one knowledge time")
                return False
        self._records.append(record)
        return True

    def records(self) -> tuple[CoverageRecord, ...]:
        return tuple(
            sorted(
                self._records,
                key=lambda item: (
                    item.source.name,
                    item.available_from_ns,
                    item.start_ns,
                    item.end_ns,
                    canonical_json_bytes(item),
                ),
            )
        )

    def summary(
        self,
        as_of_ns: int,
        start_ns: int,
        end_ns: int,
        query: ApplicabilityQuery | None,
        has_known_events: bool,
    ) -> CoverageSummary:
        require_ns(as_of_ns, "as_of_ns")
        require_ns(start_ns, "start_ns")
        require_ns(end_ns, "end_ns")
        chosen: dict[str, CoverageRecord] = {}
        for record in self._records:
            if (
                record.available_from_ns > as_of_ns
                or record.start_ns >= end_ns
                or record.end_ns <= start_ns
            ):
                continue
            if record.scope is not None and (
                query is None or not applies_directly(record.scope, query)
            ):
                continue
            previous = chosen.get(record.source.name)
            record_order = (
                record.available_from_ns,
                -(record.end_ns - record.start_ns),
                canonical_json_bytes(record),
            )
            previous_order = (
                (
                    previous.available_from_ns,
                    -(previous.end_ns - previous.start_ns),
                    canonical_json_bytes(previous),
                )
                if previous is not None
                else None
            )
            if previous_order is None or record_order > previous_order:
                chosen[record.source.name] = record
        if not chosen:
            return CoverageSummary(
                CoverageStatus.UNKNOWN if has_known_events else CoverageStatus.NOT_INGESTED
            )
        states = tuple((name, chosen[name].health.state) for name in sorted(chosen))
        partial = any(
            record.start_ns > start_ns or record.end_ns < end_ns for record in chosen.values()
        )
        healthy = any(state is SourceHealthState.HEALTHY for _, state in states)
        failed = any(
            state
            in (
                SourceHealthState.UNAVAILABLE,
                SourceHealthState.SCHEMA_MISMATCH,
                SourceHealthState.RATE_LIMITED,
                SourceHealthState.UNKNOWN,
            )
            for _, state in states
        )
        stale = any(state is SourceHealthState.STALE for _, state in states)
        degraded = any(state is SourceHealthState.DEGRADED for _, state in states)
        if failed:
            status = CoverageStatus.DEGRADED if healthy else CoverageStatus.UNAVAILABLE
        elif partial:
            status = CoverageStatus.UNKNOWN
        elif stale:
            status = CoverageStatus.DEGRADED if healthy else CoverageStatus.STALE
        elif degraded:
            status = CoverageStatus.DEGRADED
        else:
            status = CoverageStatus.AVAILABLE
        return CoverageSummary(status, states)
