"""Archive bytes and acquisition identity remain distinct."""

from dataclasses import dataclass

from eie.backfill.models import CoverageClaim, ProvenanceKind, cursor_value
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.ingestion.models import RetentionPolicy, identifier, label
from eie.sources.models import SHA256
from eie.transport.errors import TransportPolicyError
from eie.transport.models import HttpPolicy

ARCHIVE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or SHA256.fullmatch(self.sha256) is None:
            raise ValidationError("content-addressed SHA-256 required")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length < 0
        ):
            raise ValidationError("nonnegative archive byte length required")


@dataclass(frozen=True, slots=True)
class ArchiveAcquisition:
    build_id: str
    partition_id: str
    provider_id: str
    resource_id: str
    page_number: int
    source_reference: str
    acquired_at_ns: int
    object: ArchiveObject
    content_type: str
    retention: RetentionPolicy
    adapter_version: str
    parser_version: str
    next_cursor: str | None = None
    coverage: CoverageClaim = CoverageClaim.UNKNOWN
    provenance: ProvenanceKind = ProvenanceKind.HISTORICALLY_RECONSTRUCTED
    replayed_at_ns: int | None = None
    too_many_results: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.build_id,
            self.partition_id,
            self.resource_id,
            self.content_type,
            self.adapter_version,
            self.parser_version,
        ):
            label(value)
        identifier(self.provider_id)
        require_ns(self.acquired_at_ns)
        if self.replayed_at_ns is not None:
            require_ns(self.replayed_at_ns)
            if self.replayed_at_ns < self.acquired_at_ns:
                raise ValidationError("archive replay precedes original acquisition")
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise ValidationError("positive archive page number required")
        if not isinstance(self.object, ArchiveObject) or not isinstance(
            self.retention, RetentionPolicy
        ):
            raise ValidationError("typed archive object/policy required")
        if not isinstance(self.coverage, CoverageClaim) or not isinstance(
            self.provenance, ProvenanceKind
        ):
            raise ValidationError("typed acquisition provenance required")
        if not isinstance(self.too_many_results, bool):
            raise ValidationError("typed acquisition split flag required")
        if self.next_cursor is not None:
            cursor_value(self.next_cursor)
        from urllib.parse import urlsplit

        host = urlsplit(self.source_reference).hostname
        if host is None:
            raise ValidationError("trusted HTTPS archive source reference required")
        try:
            HttpPolicy((host,)).validate_url(self.source_reference)
        except TransportPolicyError as exc:
            raise ValidationError("unsafe archive source reference") from exc

    @property
    def acquisition_id(self) -> str:
        return "acq_" + fingerprint("eie:archive-acquisition:v1", self)
