"""Immutable scheduled views over canonical event revisions."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.schema import SCHEDULE_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.events.lifecycle import EventWindowState, LifecycleState
from eie.events.models import CanonicalEvent
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass


class ScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ScheduleClaim:
    """A source's current schedule assertion; None means cancellation."""

    observation_id: str
    source_class: SourceClass
    proposed_time_ns: int | None
    available_from_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, str) or not self.observation_id.startswith("obs_"):
            raise ValidationError("schedule claim requires raw observation ID")
        if not isinstance(self.source_class, SourceClass):
            raise ValidationError("schedule claim requires typed source class")
        if self.proposed_time_ns is not None:
            require_ns(self.proposed_time_ns, "proposed_time_ns")
        require_ns(self.available_from_ns, "available_from_ns")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:schedule-claim:v1", self)


@dataclass(frozen=True, slots=True)
class ScheduleRevision:
    """One schedule revision, backed by exactly one canonical event revision."""

    event: CanonicalEvent
    claims: tuple[ScheduleClaim, ...]
    selected_observation_id: str
    adjudication_reason_codes: tuple[str, ...] = ()
    schema_version: str = SCHEDULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, SCHEDULE_SCHEMA_VERSION)
        if not isinstance(self.event, CanonicalEvent) or not isinstance(self.claims, tuple):
            raise ValidationError("typed immutable schedule revision required")
        if self.event.scheduled_for_ns is None:
            raise ValidationError("scheduled event requires scheduled_for_ns")
        if not self.claims or any(not isinstance(claim, ScheduleClaim) for claim in self.claims):
            raise ValidationError("schedule claims required")
        ids = tuple(claim.observation_id for claim in self.claims)
        if ids != tuple(sorted(set(ids))):
            raise ValidationError("schedule claims must be sorted and unique by observation")
        if any(item not in self.event.evidence_observation_ids for item in ids):
            raise ValidationError("schedule claims must be canonical evidence")
        if self.selected_observation_id not in ids:
            raise ValidationError("selected claim is absent")
        if any(
            claim.available_from_ns > self.event.revision.available_from_ns for claim in self.claims
        ):
            raise TemporalConsistencyError("schedule claim follows canonical availability")
        selected = next(
            claim for claim in self.claims if claim.observation_id == self.selected_observation_id
        )
        if self.event.lifecycle is LifecycleState.CANCELLED:
            if selected.proposed_time_ns is not None:
                raise ValidationError("cancellation must select a cancellation claim")
        elif selected.proposed_time_ns != self.event.scheduled_for_ns:
            raise ValidationError("canonical schedule must equal selected source claim")
        if len({claim.proposed_time_ns for claim in self.claims}) > 1 and (
            QualityFlag.CONFLICTING_SOURCES not in self.event.quality_flags
        ):
            raise ValidationError("disagreeing schedule claims require conflict quality flag")
        if (
            not isinstance(self.adjudication_reason_codes, tuple)
            or any(
                not isinstance(code, str) or not code or code != code.strip()
                for code in self.adjudication_reason_codes
            )
            or self.adjudication_reason_codes != tuple(sorted(set(self.adjudication_reason_codes)))
        ):
            raise ValidationError("adjudication reasons must be sorted unique codes")

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def revision_id(self) -> str:
        return self.event.revision_id

    @property
    def status(self) -> ScheduleStatus:
        return (
            ScheduleStatus.CANCELLED
            if self.event.lifecycle is LifecycleState.CANCELLED
            else ScheduleStatus.ACTIVE
        )

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:schedule-revision:v1", self)


@dataclass(frozen=True, slots=True)
class ScheduledEventView:
    revision: ScheduleRevision
    as_of_ns: int

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if not isinstance(self.revision, ScheduleRevision):
            raise ValidationError("typed schedule revision required")
        if self.revision.event.revision.available_from_ns > self.as_of_ns:
            raise TemporalConsistencyError("future schedule in query result")

    @property
    def event_id(self) -> str:
        return self.revision.event_id

    @property
    def scheduled_for_ns(self) -> int:
        assert self.revision.event.scheduled_for_ns is not None
        return self.revision.event.scheduled_for_ns

    @property
    def time_until_ns(self) -> int | None:
        return (
            None
            if self.revision.status is ScheduleStatus.CANCELLED
            else self.scheduled_for_ns - self.as_of_ns
        )


class CoverageStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    NOT_INGESTED = "NOT_INGESTED"


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    status: CoverageStatus
    source_states: tuple[tuple[str, SourceHealthState], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, CoverageStatus) or not isinstance(self.source_states, tuple):
            raise ValidationError("typed coverage summary required")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], SourceHealthState)
            for item in self.source_states
        ):
            raise ValidationError("typed coverage source states required")
        names = tuple(name for name, _ in self.source_states)
        if names != tuple(sorted(set(names))):
            raise ValidationError("coverage sources must be sorted and unique")


class QueryOutcome(StrEnum):
    MATCHES = "MATCHES"
    NO_MATCH = "NO_MATCH"
    INDETERMINATE = "INDETERMINATE"


class ScheduleQueryKind(StrEnum):
    KNOWN = "KNOWN"
    BETWEEN = "BETWEEN"
    UPCOMING = "UPCOMING"
    RECENT = "RECENT"


@dataclass(frozen=True, slots=True)
class ScheduledEventQueryResult:
    kind: ScheduleQueryKind
    as_of_ns: int
    start_ns: int
    end_ns: int
    events: tuple[ScheduledEventView, ...]
    coverage: CoverageSummary
    config_fingerprint: str
    schema_version: str = SCHEDULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, SCHEDULE_SCHEMA_VERSION)
        if not isinstance(self.kind, ScheduleQueryKind):
            raise ValidationError("typed schedule query kind required")
        for name in ("as_of_ns", "start_ns", "end_ns"):
            require_ns(getattr(self, name), name)
        if self.end_ns < self.start_ns:
            raise TemporalConsistencyError("query end precedes start")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, ScheduledEventView) for event in self.events
        ):
            raise ValidationError("typed scheduled event views required")
        if not isinstance(self.coverage, CoverageSummary) or not isinstance(
            self.config_fingerprint, str
        ):
            raise ValidationError("typed query coverage and fingerprint required")
        if any(event.as_of_ns != self.as_of_ns for event in self.events):
            raise ValidationError("result contains mismatched as-of time")
        keys = tuple(
            (event.scheduled_for_ns, event.revision.event.identity.event_type.code, event.event_id)
            for event in self.events
        )
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("scheduled event views must be sorted and unique")

    @property
    def outcome(self) -> QueryOutcome:
        if self.events:
            return QueryOutcome.MATCHES
        if self.coverage.status is CoverageStatus.AVAILABLE:
            return QueryOutcome.NO_MATCH
        return QueryOutcome.INDETERMINATE

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:scheduled-query:v1", self)


@dataclass(frozen=True, slots=True)
class NextEventResult:
    as_of_ns: int
    event: ScheduledEventView | None
    coverage: CoverageSummary
    config_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if self.event is not None and (
            not isinstance(self.event, ScheduledEventView)
            or self.event.as_of_ns != self.as_of_ns
            or self.event.time_until_ns is None
            or self.event.time_until_ns < 0
        ):
            raise ValidationError("next event must be active and upcoming as of query time")
        if not isinstance(self.coverage, CoverageSummary) or not isinstance(
            self.config_fingerprint, str
        ):
            raise ValidationError("typed next-event coverage and fingerprint required")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:next-scheduled-event:v1", self)


@dataclass(frozen=True, slots=True)
class ActiveEventWindow:
    event: ScheduledEventView
    state: EventWindowState

    def __post_init__(self) -> None:
        if not isinstance(self.event, ScheduledEventView) or not isinstance(
            self.state, EventWindowState
        ):
            raise ValidationError("typed active event window required")


@dataclass(frozen=True, slots=True)
class ActiveWindowsResult:
    as_of_ns: int
    windows: tuple[ActiveEventWindow, ...]
    aggregate_state: EventWindowState
    coverage: CoverageSummary
    config_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if not isinstance(self.windows, tuple) or any(
            not isinstance(item, ActiveEventWindow) for item in self.windows
        ):
            raise ValidationError("typed active windows required")
        if not isinstance(self.aggregate_state, EventWindowState):
            raise ValidationError("typed aggregate window state required")
        if any(item.event.as_of_ns != self.as_of_ns for item in self.windows):
            raise ValidationError("active window has mismatched as-of time")
        keys = tuple(
            (
                item.event.scheduled_for_ns,
                item.event.revision.event.identity.event_type.code,
                item.event.event_id,
            )
            for item in self.windows
        )
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("active windows must be sorted and unique")
        if not isinstance(self.coverage, CoverageSummary) or not isinstance(
            self.config_fingerprint, str
        ):
            raise ValidationError("typed active-window coverage and fingerprint required")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:active-windows:v1", self)
