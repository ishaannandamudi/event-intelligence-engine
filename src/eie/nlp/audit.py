"""Read-only audit contracts; no repository or runtime import side effects."""

from dataclasses import dataclass

from eie.evidence.claims import ProviderClaimObservation
from eie.evidence.references import EvidenceReference
from eie.nlp.documents import TextDocument
from eie.nlp.mentions import EntityMention, NumericMention, PeriodMention, TemporalMention
from eie.nlp.models import ExtractedEventCandidate, ExtractionRun, VerificationResult
from eie.nlp.normalization import NormalizationMap
from eie.nlp.segmentation import TextSegment
from eie.nlp.spans import EvidenceSpan

Artifact = (
    EntityMention
    | NumericMention
    | PeriodMention
    | TemporalMention
    | ExtractedEventCandidate
    | VerificationResult
    | EvidenceSpan
)


@dataclass(frozen=True, slots=True)
class TextAudit:
    document: TextDocument
    run: ExtractionRun
    mapping: NormalizationMap
    segments: tuple[TextSegment, ...]
    mentions: tuple[Artifact, ...]
    candidates: tuple[ExtractedEventCandidate, ...]
    verifications: tuple[VerificationResult, ...]
    spans: tuple[EvidenceSpan, ...]
    assertions: tuple[ProviderClaimObservation, ...]
    evidence: tuple[EvidenceReference, ...]
