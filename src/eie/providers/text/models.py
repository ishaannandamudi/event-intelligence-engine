"""Trusted source configuration; text cannot grant retention or source authority."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.nlp.checks import checked
from eie.nlp.models import ExtractionSettings
from eie.providers.evidence.models import EvidenceProvider


@dataclass(frozen=True, slots=True)
class TextProvider:
    evidence: EvidenceProvider
    settings: ExtractionSettings = ExtractionSettings()
    timezone: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        checked(self)
        # The acquisition parser remains pinned by EIE-7/EIE-11; reprocessing versions
        # live in settings and the processing manifest, never forged source metadata.
        if self.evidence.parser_version == "":
            raise ValidationError("configured source parser required")
