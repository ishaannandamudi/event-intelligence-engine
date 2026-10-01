"""Operational contracts; never included in canonical event fingerprints."""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns

INGESTION_SCHEMA_VERSION = "1.0"


def identifier(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", value) is None:
        raise ValidationError("stable operational provider ID required")


def label(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(c in value for c in "\r\n\x00")
    ):
        raise ValidationError("safe operational label required")


class CursorKind(StrEnum):
    SOURCE_ITEM = "SOURCE_ITEM"
    ACCESSION = "ACCESSION"
    TIMESTAMP = "TIMESTAMP"
    SEQUENCE = "SEQUENCE"
    OPAQUE = "OPAQUE"


@dataclass(frozen=True, slots=True)
class Cursor:
    kind: CursorKind
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CursorKind):
            raise ValidationError("typed cursor kind required")
        label(self.value)
        if len(self.value) > 4096:
            raise ValidationError("cursor exceeds bound")
        if self.kind in (CursorKind.TIMESTAMP, CursorKind.SEQUENCE) and (
            not self.value.isascii() or not self.value.isdecimal()
        ):
            raise ValidationError("numeric cursor required")
        if self.kind is CursorKind.TIMESTAMP:
            require_ns(int(self.value))
        if (
            self.kind is CursorKind.ACCESSION
            and re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", self.value) is None
        ):
            raise ValidationError("canonical accession cursor required")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    provider_id: str
    resource_id: str
    generation: int
    observed_ns: int
    completed_ns: int
    cursor: Cursor | None = None
    last_source_item: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    schema_version: str = INGESTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifier(self.provider_id)
        label(self.resource_id)
        for value in (self.observed_ns, self.completed_ns):
            require_ns(value)
        if (
            self.completed_ns < self.observed_ns
            or isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValidationError("invalid checkpoint generation/clocks")
        if self.schema_version != INGESTION_SCHEMA_VERSION or (
            self.cursor is not None and not isinstance(self.cursor, Cursor)
        ):
            raise ValidationError("unsupported checkpoint contract")
        for text_value in (self.last_source_item, self.etag, self.last_modified):
            if text_value is not None:
                label(text_value)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:checkpoint:v1", self)


class FailureClass(StrEnum):
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    RATE_LIMIT = "RATE_LIMIT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARSE_FAILURE = "PARSE_FAILURE"
    NORMALIZATION_FAILURE = "NORMALIZATION_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    CHECKPOINT_FAILURE = "CHECKPOINT_FAILURE"
    CONFIGURATION_FAILURE = "CONFIGURATION_FAILURE"
    BACKPRESSURE = "BACKPRESSURE"


class RuntimeHealthState(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    provider_id: str
    available_ns: int
    state: RuntimeHealthState
    last_attempt_ns: int | None = None
    last_success_ns: int | None = None
    consecutive_failures: int = 0
    error_category: FailureClass | None = None

    def __post_init__(self) -> None:
        identifier(self.provider_id)
        require_ns(self.available_ns)
        if not isinstance(self.state, RuntimeHealthState) or (
            self.error_category is not None and not isinstance(self.error_category, FailureClass)
        ):
            raise ValidationError("typed runtime health required")
        for value in (self.last_attempt_ns, self.last_success_ns):
            if value is not None and require_ns(value) > self.available_ns:
                raise ValidationError("future operational health")
        if (
            isinstance(self.consecutive_failures, bool)
            or not isinstance(self.consecutive_failures, int)
            or self.consecutive_failures < 0
        ):
            raise ValidationError("invalid runtime failure count")


@dataclass(frozen=True, slots=True)
class Latencies:
    fetch_ns: int = 0
    queue_ns: int = 0
    prepare_ns: int = 0
    commit_ns: int = 0
    total_ns: int = 0
    parse_ns: int = 0
    normalize_ns: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.fetch_ns,
            self.queue_ns,
            self.prepare_ns,
            self.commit_ns,
            self.total_ns,
            self.parse_ns,
            self.normalize_ns,
        ):
            require_ns(value)


@dataclass(frozen=True, slots=True)
class PollResult:
    provider_id: str
    resource_id: str
    observed_ns: int
    completed_ns: int
    status: int | None
    committed: bool
    items_ingested: int = 0
    duplicates: int = 0
    checkpoint_generation: int = 0
    quarantined: bool = False
    error_category: FailureClass | None = None
    latencies: Latencies = Latencies()
    items_fetched: int = 0
    raw_created: int = 0
    domain_created: int = 0

    def __post_init__(self) -> None:
        identifier(self.provider_id)
        label(self.resource_id)
        require_ns(self.observed_ns)
        require_ns(self.completed_ns)
        if self.completed_ns < self.observed_ns or not isinstance(self.latencies, Latencies):
            raise ValidationError("invalid poll timing")
        for count in (
            self.items_ingested,
            self.duplicates,
            self.checkpoint_generation,
            self.items_fetched,
            self.raw_created,
            self.domain_created,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValidationError("invalid poll counter")
        if (
            not isinstance(self.committed, bool)
            or not isinstance(self.quarantined, bool)
            or (
                self.error_category is not None
                and not isinstance(self.error_category, FailureClass)
            )
            or (
                self.status is not None
                and (
                    isinstance(self.status, bool)
                    or not isinstance(self.status, int)
                    or not 100 <= self.status <= 599
                )
            )
        ):
            raise ValidationError("typed poll outcome required")


@dataclass(frozen=True, slots=True)
class Quarantine:
    provider_id: str
    resource_id: str
    payload_hash: str
    adapter_version: str
    category: FailureClass
    first_failure_ns: int
    last_failure_ns: int
    attempts: int = 1

    def __post_init__(self) -> None:
        identifier(self.provider_id)
        label(self.resource_id)
        label(self.adapter_version)
        require_ns(self.first_failure_ns)
        require_ns(self.last_failure_ns)
        if (
            self.last_failure_ns < self.first_failure_ns
            or re.fullmatch(r"[0-9a-f]{64}", self.payload_hash) is None
            or isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 1
            or not isinstance(self.category, FailureClass)
        ):
            raise ValidationError("invalid quarantine metadata")


class RetentionMode(StrEnum):
    METADATA_ONLY = "METADATA_ONLY"
    BOUNDED_RAW = "BOUNDED_RAW"


class RetentionPermission(StrEnum):
    ALLOWED = "ALLOWED"
    RESTRICTED = "RESTRICTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    mode: RetentionMode = RetentionMode.METADATA_ONLY
    permission: RetentionPermission = RetentionPermission.UNKNOWN
    max_raw_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, RetentionMode)
            or not isinstance(self.permission, RetentionPermission)
            or isinstance(self.max_raw_bytes, bool)
            or not isinstance(self.max_raw_bytes, int)
            or self.max_raw_bytes < 1
        ):
            raise ValidationError("invalid raw retention policy")
        if (
            self.mode is RetentionMode.BOUNDED_RAW
            and self.permission is not RetentionPermission.ALLOWED
        ):
            raise ValidationError("raw retention requires explicit permission")
