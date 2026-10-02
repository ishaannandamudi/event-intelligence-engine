"""Hash-pinned structured locators; raw bodies and fragments are never embedded."""

from dataclasses import dataclass
from enum import StrEnum

from eie.backfill.models import ProvenanceKind, ReconstructionQuality
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import identity
from eie.studies.checks import contract, digest


class LocatorKind(StrEnum):
    FIELD = "FIELD"
    JSON_POINTER = "JSON_POINTER"
    CSV_FIELD = "CSV_FIELD"
    XML_PATH = "XML_PATH"
    SEC_METADATA = "SEC_METADATA"


class SpanKind(StrEnum):
    BYTE = "BYTE"
    CHARACTER = "CHARACTER"


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    kind: LocatorKind
    path: str
    row: int | None = None
    span_kind: SpanKind | None = None
    start: int | None = None
    end: int | None = None
    content_length: int | None = None

    def __post_init__(self) -> None:
        contract(self)
        if self.kind is LocatorKind.JSON_POINTER and not self.path.startswith("/"):
            raise ValidationError("structured JSON pointer required")
        if self.kind is LocatorKind.XML_PATH and not self.path.startswith("/"):
            raise ValidationError("static XML location required")
        if (
            (self.kind is LocatorKind.CSV_FIELD) != (self.row is not None)
            or self.row is not None
            and self.row < 0
        ):
            raise ValidationError("CSV locator requires nonnegative row")
        if self.span_kind is None:
            if any(v is not None for v in (self.start, self.end, self.content_length)):
                raise ValidationError("span coordinates need explicit encoding semantics")
        elif (
            self.start is None
            or self.end is None
            or self.content_length is None
            or not 0 <= self.start < self.end <= self.content_length <= 100_000_000
        ):
            raise ValidationError("safe bounded half-open evidence span required")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    raw_observation_id: str
    source_item_id: str
    content_hash: str
    fragment_hash: str
    parser_version: str
    adapter_version: str
    locator: EvidenceLocator

    def __post_init__(self) -> None:
        contract(self)
        identity(self.raw_observation_id, ("obs",))
        digest(self.content_hash)
        digest(self.fragment_hash)

    @property
    def evidence_id(self) -> str:
        return "eref_" + fingerprint("eie:evidence-reference:v1", self)


@dataclass(frozen=True, slots=True)
class EvidenceDataset:
    namespace: str
    fingerprint: str
    build_id: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)

    @property
    def dataset_id(self) -> str:
        return "eds_" + fingerprint("eie:evidence-dataset:v1", self)


@dataclass(frozen=True, slots=True)
class AcquisitionLineage:
    raw_observation_id: str
    observed_at_ns: int
    ingested_at_ns: int
    parser_version: str
    settings_fingerprint: str
    provenance: ProvenanceKind = ProvenanceKind.LIVE_OBSERVED
    reconstruction: ReconstructionQuality = ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME
    source_available_ns: int | None = None
    build_id: str | None = None
    historical_fact_id: str | None = None
    historical_acquisition_id: str | None = None
    checkpoint: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        identity(self.raw_observation_id, ("obs",))
        digest(self.settings_fingerprint)
        if self.observed_at_ns > self.ingested_at_ns:
            raise ValidationError("acquisition clocks out of order")
        if self.provenance is ProvenanceKind.LIVE_OBSERVED:
            if (
                self.source_available_ns is not None
                or self.build_id is not None
                or self.historical_fact_id is not None
                or self.historical_acquisition_id is not None
            ):
                raise ValidationError("live acquisition cannot pretend historical reconstruction")
        elif (
            self.build_id is None
            or self.historical_fact_id is None
            or self.historical_acquisition_id is None
        ):
            raise ValidationError("reconstructed acquisition must pin its historical build/fact")
        exact = self.reconstruction in (
            ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
            ReconstructionQuality.SOURCE_TIMESTAMP_RECONSTRUCTED,
        )
        if self.provenance is not ProvenanceKind.LIVE_OBSERVED and exact != (
            self.source_available_ns is not None
        ):
            raise ValidationError("reconstruction availability must preserve precision")
        if self.source_available_ns is not None and self.source_available_ns > self.observed_at_ns:
            raise ValidationError("historical availability follows acquisition")

    @property
    def lineage_id(self) -> str:
        return "eacq_" + fingerprint("eie:evidence-acquisition:v1", self)

    @property
    def known_from_ns(self) -> int:
        return self.ingested_at_ns if self.source_available_ns is None else self.source_available_ns
