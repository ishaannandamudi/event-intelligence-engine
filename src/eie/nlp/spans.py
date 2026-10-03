"""Grounding maps normalized offsets back to exact original character ranges."""

from dataclasses import dataclass
from hashlib import sha256

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import identity
from eie.nlp.checks import checked
from eie.nlp.normalization import NormalizedText
from eie.nlp.segmentation import TextSegment
from eie.studies.checks import digest


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    document_id: str
    segment_id: str
    map_id: str
    start: int
    end: int
    original_start: int
    original_end: int
    normalized_fragment_hash: str
    original_fragment_hash: str

    def __post_init__(self) -> None:
        checked(self)
        identity(self.document_id, ("tdoc",))
        identity(self.segment_id, ("tseg",))
        identity(self.map_id, ("tmap",))
        digest(self.normalized_fragment_hash)
        digest(self.original_fragment_hash)
        if not (0 <= self.start < self.end and 0 <= self.original_start < self.original_end):
            raise ValidationError("positive evidence span required")

    @property
    def span_id(self) -> str:
        return "tspan_" + fingerprint("eie:text-evidence-span:v1", self)


def make_span(
    original: str, normalized: NormalizedText, segment: TextSegment, start: int, end: int
) -> EvidenceSpan:
    if (
        not segment.start <= start < end <= segment.end
        or segment.map_id != normalized.mapping.map_id
        or sha256(original.encode()).hexdigest() != normalized.mapping.original_hash
    ):
        raise ValidationError("source span segment/mapping mismatch")
    a, b = normalized.mapping.original_range(start, end)
    return EvidenceSpan(
        segment.document_id,
        segment.segment_id,
        segment.map_id,
        start,
        end,
        a,
        b,
        sha256(normalized.text[start:end].encode()).hexdigest(),
        sha256(original[a:b].encode()).hexdigest(),
    )


def verify_span(
    span: EvidenceSpan, original: str, normalized: NormalizedText, segments: tuple[TextSegment, ...]
) -> None:
    selected = next((s for s in segments if s.segment_id == span.segment_id), None)
    if selected is None or make_span(original, normalized, selected, span.start, span.end) != span:
        raise ValidationError("evidence span source hash/offset/membership mismatch")
