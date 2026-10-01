"""Immutable raw observations and typed source health."""

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.schema import OBSERVATION_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.sources.trust import SourceClass

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "token",
        "secret",
        "password",
        "authorization",
        "signature",
        "sig",
    }
)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    name: str
    source_version: str
    adapter_version: str
    source_class: SourceClass

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, SourceClass):
            raise ValidationError("typed source class required")
        if not SOURCE_NAME.fullmatch(self.name):
            raise ValidationError("source name must be a stable credential-free identifier")
        for field in (self.name, self.source_version, self.adapter_version):
            if not field or field != field.strip() or "\n" in field:
                raise ValidationError("source identity fields must be nonempty and trimmed")


@dataclass(frozen=True, slots=True)
class RawSourceObservation:
    source: SourceIdentity
    source_item_id: str
    source_item_version: str
    payload_hash: str
    content_type: str
    source_observed_at_ns: int
    ingested_at_ns: int
    published_at_ns: int | None = None
    source_reference: str | None = None
    raw_event_type: str | None = None
    raw_entity_identifiers: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, OBSERVATION_SCHEMA_VERSION)
        if (
            not isinstance(self.source, SourceIdentity)
            or not isinstance(self.raw_entity_identifiers, tuple)
            or not isinstance(self.metadata, tuple)
            or not isinstance(self.quality_flags, frozenset)
        ):
            raise ValidationError("typed immutable raw observation fields required")
        if (
            any(not isinstance(item, str) for item in self.raw_entity_identifiers)
            or any(not isinstance(item, QualityFlag) for item in self.quality_flags)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(part, str) for part in item)
                for item in self.metadata
            )
        ):
            raise ValidationError("invalid raw observation collection member")
        for field in (self.source_item_id, self.source_item_version, self.content_type):
            if not field or field != field.strip():
                raise ValidationError(
                    "raw observation requires trimmed item identity and content type"
                )
        if not SHA256.fullmatch(self.payload_hash):
            raise ValidationError("payload_hash must be lowercase SHA-256 hex")
        require_ns(self.source_observed_at_ns, "source_observed_at_ns")
        require_ns(self.ingested_at_ns, "ingested_at_ns")
        if self.published_at_ns is not None:
            require_ns(self.published_at_ns, "published_at_ns")
            if self.published_at_ns > self.source_observed_at_ns:
                raise TemporalConsistencyError("publication follows observation")
        if self.source_observed_at_ns > self.ingested_at_ns:
            raise TemporalConsistencyError("observation follows ingestion")
        if tuple(sorted(set(self.raw_entity_identifiers))) != self.raw_entity_identifiers:
            raise ValidationError("raw entity identifiers must be sorted and unique")
        if any(not item for item in self.raw_entity_identifiers):
            raise ValidationError("empty raw entity identifier")
        if tuple(sorted({key for key, _ in self.metadata})) != tuple(
            key for key, _ in self.metadata
        ):
            raise ValidationError("metadata keys must be sorted and unique")
        if any(not key for key, _ in self.metadata):
            raise ValidationError("empty metadata key")
        if any(key.lower() in SECRET_KEYS for key, _ in self.metadata):
            raise ValidationError("credential metadata is forbidden")
        if self.source_reference is not None and "://" in self.source_reference:
            parsed = urlsplit(self.source_reference)
            if parsed.username is not None or parsed.password is not None:
                raise ValidationError("credential-bearing source reference is forbidden")
            if any(key.lower() in SECRET_KEYS for key, _ in parse_qsl(parsed.query)):
                raise ValidationError("credential-bearing source reference is forbidden")

    @property
    def observation_id(self) -> str:
        return "obs_" + fingerprint(
            "eie:observation-identity:v1",
            (self.source, self.source_item_id, self.source_item_version, self.payload_hash),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:raw-observation:v1", self)


class SourceHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    state: SourceHealthState
    last_success_ns: int | None = None
    last_attempt_ns: int | None = None
    latency_ns: int | None = None
    parse_failures: int = 0
    error_category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SourceHealthState):
            raise ValidationError("typed source health state required")
        for name in ("last_success_ns", "last_attempt_ns", "latency_ns"):
            value = getattr(self, name)
            if value is not None:
                require_ns(value, name)
        if (
            isinstance(self.parse_failures, bool)
            or not isinstance(self.parse_failures, int)
            or self.parse_failures < 0
        ):
            raise ValidationError("parse_failures must be nonnegative")
        if (
            self.last_success_ns is not None
            and self.last_attempt_ns is not None
            and self.last_success_ns > self.last_attempt_ns
        ):
            raise TemporalConsistencyError("last success follows last attempt")


@dataclass(frozen=True, slots=True)
class SourcePollResult:
    """An empty healthy result means no events; failure is a different state."""

    observations: tuple[RawSourceObservation, ...]
    health: SourceHealth

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple) or not isinstance(self.health, SourceHealth):
            raise ValidationError("typed source poll result required")
        if any(not isinstance(item, RawSourceObservation) for item in self.observations):
            raise ValidationError("poll result contains non-observation")
        if self.observations and self.health.state in (
            SourceHealthState.UNAVAILABLE,
            SourceHealthState.SCHEMA_MISMATCH,
            SourceHealthState.RATE_LIMITED,
        ):
            raise ValidationError("failed source cannot claim successful observations")
