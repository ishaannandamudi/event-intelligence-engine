"""Deterministic offset-preserving paragraphs; table cells are never inferred."""

import re
from dataclasses import dataclass
from hashlib import sha256

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import identity
from eie.nlp.checks import checked
from eie.nlp.normalization import NormalizedText
from eie.nlp.schema import SegmentKind
from eie.studies.checks import digest


@dataclass(frozen=True, slots=True)
class TextSegment:
    document_id: str
    map_id: str
    index: int
    start: int
    end: int
    content_hash: str
    kind: SegmentKind

    def __post_init__(self) -> None:
        checked(self)
        identity(self.document_id, ("tdoc",))
        identity(self.map_id, ("tmap",))
        digest(self.content_hash)
        if self.index < 0 or not 0 <= self.start < self.end:
            raise ValidationError("valid segment index/offsets required")

    @property
    def segment_id(self) -> str:
        return "tseg_" + fingerprint("eie:text-segment:v1", self)


def segment(document_id: str, text: NormalizedText) -> tuple[TextSegment, ...]:
    matches = tuple(m for m in re.finditer(r"[^\n]+", text.text) if m.group().strip())
    result = tuple(
        TextSegment(
            document_id,
            text.mapping.map_id,
            i,
            m.start(),
            m.end(),
            sha256(m.group().encode()).hexdigest(),
            SegmentKind.HEADLINE if i == 0 else SegmentKind.PARAGRAPH,
        )
        for i, m in enumerate(matches)
    )
    if len(result) > text.mapping.settings.limits.segments:
        raise ValidationError("document segment bound exceeded")
    return result
