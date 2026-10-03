"""Known SEC identity/history snapshots; names remain candidates and roles stay lightweight."""

import re
from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.entities.protocols import ResolutionState
from eie.entities.sec import SecEntityRegistry, SecIssuerRevision, SecListingRevision
from eie.nlp.checks import checked
from eie.nlp.mentions import EntityMention
from eie.nlp.normalization import NormalizedText
from eie.nlp.schema import EntityMentionKind
from eie.nlp.segmentation import TextSegment
from eie.nlp.spans import make_span


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    issuers: tuple[SecIssuerRevision, ...] = ()
    listings: tuple[SecListingRevision, ...] = ()

    def __post_init__(self) -> None:
        checked(self)
        if (
            len(self.issuers) > 10000
            or len(self.listings) > 10000
            or tuple(r.issuer_id for r in self.issuers)
            != tuple(sorted({r.issuer_id for r in self.issuers}))
        ):
            raise ValidationError("bounded distinct issuer snapshot required")
        if tuple(r.listing_id for r in self.listings) != tuple(
            sorted({r.listing_id for r in self.listings})
        ) or any(r.issuer_id not in {i.issuer_id for i in self.issuers} for r in self.listings):
            raise ValidationError("known distinct listing snapshot required")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:text-entity-catalog:v1", self)

    @classmethod
    def from_registry(
        cls,
        registry: SecEntityRegistry,
        issuer_ids: tuple[str, ...],
        known_as_of_ns: int,
        valid_at_ns: int,
    ) -> "EntityCatalog":
        issuers = tuple(
            r
            for i in sorted(set(issuer_ids))
            if (r := registry.issuer_by_id(i, known_as_of_ns)) is not None
        )
        listings = tuple(
            sorted(
                (
                    r
                    for i in issuers
                    for r in registry.listings_at(i.issuer_id, known_as_of_ns, valid_at_ns)
                ),
                key=lambda r: r.listing_id,
            )
        )
        return cls(issuers, listings)


def extract_entities(
    original: str,
    normalized: NormalizedText,
    segments: tuple[TextSegment, ...],
    catalog: EntityCatalog,
    known_as_of_ns: int,
    valid_at_ns: int,
) -> tuple[EntityMention, ...]:
    result: list[EntityMention] = []
    issuers = tuple(i for i in catalog.issuers if i.available_from_ns <= known_as_of_ns)
    known_issuers = {i.issuer_id for i in issuers}
    for seg in segments:
        text = normalized.text[seg.start : seg.end]
        for m in re.finditer(
            r"\bCIK\s*[:#]?\s*(\d{1,10})(?!\d)|(?:\b(?:NASDAQ|NYSE|ticker)\s*:\s*|\$)([A-Z][A-Z0-9.\-]{0,19})(?![A-Za-z0-9])",
            text,
        ):
            cik, ticker = m.groups()
            if cik is not None:
                identifier = cik.zfill(10)
                rows = tuple(i for i in issuers if i.cik.digits == identifier)
                candidates = tuple(sorted({i.issuer_id for i in rows}))
                authority = tuple(sorted(i.result_fingerprint for i in rows))
                kind = EntityMentionKind.CIK
            else:
                identifier = ticker
                listing_rows = tuple(
                    r
                    for r in catalog.listings
                    if r.ticker == ticker
                    and r.issuer_id in known_issuers
                    and r.available_from_ns <= known_as_of_ns
                    and r.valid_from_ns <= valid_at_ns
                    and (r.valid_to_ns is None or valid_at_ns < r.valid_to_ns)
                )
                candidates = tuple(sorted({r.issuer_id for r in listing_rows}))
                authority = tuple(sorted(r.result_fingerprint for r in listing_rows))
                kind = EntityMentionKind.TICKER
            state = (
                ResolutionState.RESOLVED
                if len(candidates) == 1
                else ResolutionState.AMBIGUOUS
                if candidates
                else ResolutionState.UNRESOLVED
            )
            result.append(
                EntityMention(
                    make_span(
                        original, normalized, seg, seg.start + m.start(), seg.start + m.end()
                    ),
                    kind,
                    identifier,
                    candidates,
                    state,
                    "SEC_EXACT_IDENTIFIER",
                    authority,
                )
            )
        names = sorted({name for i in issuers for name in (i.legal_name, *i.aliases)})
        for name in names:
            for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
                candidates = tuple(
                    sorted({i.issuer_id for i in issuers if name in (i.legal_name, *i.aliases)})
                )
                result.append(
                    EntityMention(
                        make_span(
                            original, normalized, seg, seg.start + m.start(), seg.start + m.end()
                        ),
                        EntityMentionKind.NAME,
                        name,
                        candidates,
                        ResolutionState.AMBIGUOUS
                        if len(candidates) > 1
                        else ResolutionState.UNRESOLVED,
                        "NAME_CANDIDATE_ONLY",
                    )
                )
        for m in re.finditer(
            r"\b(CEO|CFO|director|auditor)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text
        ):
            result.append(
                EntityMention(
                    make_span(
                        original, normalized, seg, seg.start + m.start(), seg.start + m.end()
                    ),
                    EntityMentionKind.PERSON_ROLE,
                    m.group(2),
                    (),
                    ResolutionState.UNRESOLVED,
                    "EXPLICIT_PERSON_ROLE",
                    role=m.group(1),
                )
            )
    if len(result) > normalized.mapping.settings.limits.mentions:
        raise ValidationError("entity mention bound exceeded")
    return tuple(sorted(result, key=lambda r: r.mention_id))
