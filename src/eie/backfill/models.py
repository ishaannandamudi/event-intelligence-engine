"""Immutable historical capabilities, plans and operational progress."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.ingestion.models import identifier, label
from eie.runtime.config import positive_int
from eie.sources.trust import SourceClass

BACKFILL_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        require_ns(self.start_ns)
        require_ns(self.end_ns)
        if self.end_ns <= self.start_ns:
            raise ValidationError("historical ranges must be nonempty [start,end)")

    def contains(self, value: int) -> bool:
        return self.start_ns <= require_ns(value) < self.end_ns


class CoverageClaim(StrEnum):
    EXHAUSTIVE = "EXHAUSTIVE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class ReconstructionQuality(StrEnum):
    AUTHORITATIVE_POINT_IN_TIME = "AUTHORITATIVE_POINT_IN_TIME"
    SOURCE_TIMESTAMP_RECONSTRUCTED = "SOURCE_TIMESTAMP_RECONSTRUCTED"
    DATE_ONLY_RECONSTRUCTED = "DATE_ONLY_RECONSTRUCTED"
    CURRENT_SNAPSHOT_ONLY = "CURRENT_SNAPSHOT_ONLY"
    UNKNOWN = "UNKNOWN"


class ProvenanceKind(StrEnum):
    LIVE_OBSERVED = "LIVE_OBSERVED"
    HISTORICALLY_RECONSTRUCTED = "HISTORICALLY_RECONSTRUCTED"
    ARCHIVE_REPLAYED = "ARCHIVE_REPLAYED"


class GapReason(StrEnum):
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNSUPPORTED_HISTORY = "UNSUPPORTED_HISTORY"
    NO_POINT_IN_TIME_HISTORY = "NO_POINT_IN_TIME_HISTORY"
    MISSING_PARTITION = "MISSING_PARTITION"
    PAGINATION_FAILURE = "PAGINATION_FAILURE"
    ARCHIVE_FAILURE = "ARCHIVE_FAILURE"
    PARSE_FAILURE = "PARSE_FAILURE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARTIAL_PROVIDER_COVERAGE = "PARTIAL_PROVIDER_COVERAGE"
    UNKNOWN_COMPLETENESS = "UNKNOWN_COMPLETENESS"
    LICENSE_RESTRICTION = "LICENSE_RESTRICTION"
    TEMPORAL_AMBIGUITY = "TEMPORAL_AMBIGUITY"
    ENTITY_UNRESOLVED = "ENTITY_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class HistoricalCapability:
    max_range_ns: int = 86_400_000_000_000
    max_pages: int = 100
    max_items: int = 10_000
    max_payload_bytes: int = 2_000_000
    max_split_depth: int = 8
    earliest_ns: int = 0
    latest_ns: int | None = None
    point_in_time: bool = True
    supported: bool = True
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_range_ns, 2**63 - 1),
            (self.max_pages, 10_000),
            (self.max_items, 100_000),
            (self.max_payload_bytes, 100_000_000),
        ):
            positive_int(value, maximum)
        if (
            isinstance(self.max_split_depth, bool)
            or not isinstance(self.max_split_depth, int)
            or not 0 <= self.max_split_depth <= 16
        ):
            raise ValidationError("bounded split depth required")
        require_ns(self.earliest_ns)
        if self.latest_ns is not None and require_ns(self.latest_ns) <= self.earliest_ns:
            raise ValidationError("historical capability bounds reversed")
        if not isinstance(self.supported, bool) or not isinstance(self.point_in_time, bool):
            raise ValidationError("typed historical capability flags required")
        sorted_labels(self.assumptions)


def sorted_labels(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
        raise ValidationError("sorted unique immutable labels required")
    for value in values:
        label(value)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    provider_id: str
    resources: tuple[str, ...]
    adapter_version: str
    parser_version: str
    capability: HistoricalCapability = HistoricalCapability()
    source_class: SourceClass = SourceClass.UNVERIFIED
    identity_bindings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        identifier(self.provider_id)
        sorted_labels(self.resources)
        if not self.resources or not isinstance(self.capability, HistoricalCapability):
            raise ValidationError("historical source requires resources/capability")
        label(self.adapter_version)
        label(self.parser_version)
        if not isinstance(self.source_class, SourceClass):
            raise ValidationError("typed configured historical source class required")
        if not isinstance(self.identity_bindings, tuple) or self.identity_bindings != tuple(
            sorted(set(self.identity_bindings))
        ):
            raise ValidationError("sorted unique pinned identity bindings required")
        if len({resource for resource, _ in self.identity_bindings}) != len(self.identity_bindings):
            raise ValidationError("one identity binding per resource required")
        for resource, identity in self.identity_bindings:
            if resource not in self.resources:
                raise ValidationError("identity binding requires a configured resource")
            label(identity)


@dataclass(frozen=True, slots=True)
class PartitionPolicy:
    span_ns: int = 86_400_000_000_000
    max_partitions: int = 100_000

    def __post_init__(self) -> None:
        positive_int(self.span_ns, 2**63 - 1)
        positive_int(self.max_partitions, 1_000_000)


@dataclass(frozen=True, slots=True)
class Partition:
    job_id: str
    provider_id: str
    resource_id: str
    interval: TimeRange
    parent_id: str | None = None
    depth: int = 0

    def __post_init__(self) -> None:
        label(self.job_id)
        identifier(self.provider_id)
        label(self.resource_id)
        if not isinstance(self.interval, TimeRange):
            raise ValidationError("typed partition range required")
        if (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or not 0 <= self.depth <= 16
        ):
            raise ValidationError("invalid partition depth")
        if (self.parent_id is None) != (self.depth == 0):
            raise ValidationError("split parent/depth disagree")
        if self.parent_id is not None:
            label(self.parent_id)

    @property
    def partition_id(self) -> str:
        return "part_" + fingerprint("eie:historical-partition:v1", self)


class JobStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_GAPS = "COMPLETED_WITH_GAPS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PartitionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    KNOWN_EMPTY = "KNOWN_EMPTY"
    GAPPED = "GAPPED"
    FAILED = "FAILED"
    SPLIT = "SPLIT"


@dataclass(frozen=True, slots=True)
class PartitionProgress:
    partition_id: str
    status: PartitionStatus = PartitionStatus.PENDING
    cursor: str | None = None
    visited_cursors: tuple[str, ...] = ()
    pages: int = 0
    items_seen: int = 0
    items_committed: int = 0
    duplicates: int = 0
    attempts: int = 0
    failures: int = 0
    content_fingerprint: str | None = None

    def __post_init__(self) -> None:
        label(self.partition_id)
        if not isinstance(self.status, PartitionStatus):
            raise ValidationError("typed partition status required")
        if self.cursor is not None:
            cursor_value(self.cursor)
        if not isinstance(self.visited_cursors, tuple):
            raise ValidationError("immutable cursor chain required")
        for value in self.visited_cursors:
            cursor_value(value)
        if len(set(self.visited_cursors)) != len(self.visited_cursors):
            raise ValidationError("circular historical cursor chain")
        for count in (
            self.pages,
            self.items_seen,
            self.items_committed,
            self.duplicates,
            self.attempts,
            self.failures,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValidationError("nonnegative historical progress required")


def cursor_value(value: str) -> None:
    label(value)
    if len(value) > 32_768:
        raise ValidationError("historical cursor exceeds bound")


@dataclass(frozen=True, slots=True)
class GapRecord:
    partition_id: str
    provider_id: str
    resource_id: str
    interval: TimeRange
    reason: GapReason
    discovered_ns: int
    retryable: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        label(self.partition_id)
        identifier(self.provider_id)
        label(self.resource_id)
        require_ns(self.discovered_ns)
        if not isinstance(self.reason, GapReason) or not isinstance(self.interval, TimeRange):
            raise ValidationError("typed historical gap required")
        if not isinstance(self.retryable, bool):
            raise ValidationError("typed gap retryability required")
        sorted_labels(self.evidence)

    @property
    def gap_id(self) -> str:
        return "gap_" + fingerprint("eie:historical-gap:v1", self.semantic_content())

    def semantic_content(self) -> object:
        return (
            self.provider_id,
            self.resource_id,
            self.interval,
            self.reason,
            self.retryable,
            self.evidence,
        )
