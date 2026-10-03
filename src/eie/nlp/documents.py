"""Documents retain source metadata once; source revisions do not rewrite extraction."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import identity
from eie.evidence.references import AcquisitionLineage
from eie.ingestion.models import RetentionPolicy
from eie.nlp import TEXT_EXTRACTION_SCHEMA_VERSION
from eie.nlp.checks import checked
from eie.nlp.schema import DocumentType, SourceAction, TextFormat
from eie.sources.models import RawSourceObservation


@dataclass(frozen=True, slots=True)
class TextDocument:
    raw: RawSourceObservation
    dataset_id: str
    resource_id: str
    source_family: str
    document_type: DocumentType
    format: TextFormat
    language: str
    source_lineage: AcquisitionLineage
    retention: RetentionPolicy
    action: SourceAction = SourceAction.ORIGINAL
    title: str | None = None
    timezone: str | None = None
    currency: str | None = None
    previous_document_id: str | None = None
    schema_version: str = TEXT_EXTRACTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        checked(self)
        identity(self.dataset_id, ("eds",))
        if self.schema_version != TEXT_EXTRACTION_SCHEMA_VERSION:
            raise ValidationError("supported text extraction schema required")
        if self.raw.observation_id != self.source_lineage.raw_observation_id or (
            self.raw.source_observed_at_ns,
            self.raw.ingested_at_ns,
        ) != (self.source_lineage.observed_at_ns, self.source_lineage.ingested_at_ns):
            raise ValidationError("document source acquisition lineage disagrees")
        if self.previous_document_id is not None:
            identity(self.previous_document_id, ("tdoc",))
        if self.currency is not None:
            import re

            if re.fullmatch(r"[A-Z]{3}", self.currency) is None:
                raise ValidationError("explicit source currency required")
        if self.action is not SourceAction.ORIGINAL and self.previous_document_id is None:
            raise ValidationError("source change requires an earlier document")
        if self.timezone is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(self.timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValidationError("configured source timezone required") from exc

    @property
    def thread_id(self) -> str:
        return "dthread_" + fingerprint(
            "eie:text-document-thread:v1",
            (self.dataset_id, self.raw.source.name, self.resource_id, self.raw.source_item_id),
        )

    @property
    def document_id(self) -> str:
        return "tdoc_" + fingerprint("eie:text-document:v1", self)


@dataclass(frozen=True, slots=True)
class ExtractionAcquisition(AcquisitionLineage):
    """Adds processing availability without changing any EIE-11 source contract."""

    extraction_available_ns: int = 0
    processing_fingerprint: str = "0" * 64

    def __post_init__(self) -> None:
        AcquisitionLineage.__post_init__(self)
        from eie.studies.checks import digest

        digest(self.processing_fingerprint)
        if self.extraction_available_ns < self.ingested_at_ns:
            raise ValidationError("extraction availability precedes actual acquisition")

    @property
    def known_from_ns(self) -> int:
        source_known = (
            self.ingested_at_ns if self.source_available_ns is None else self.source_available_ns
        )
        return max(source_known, self.extraction_available_ns)
