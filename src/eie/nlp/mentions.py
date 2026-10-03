"""Typed extracted mentions carry exact spans, never opaque confidence scores."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.corporate.types import EarningsPeriod
from eie.entities.protocols import ResolutionState
from eie.evidence.claims import TimeValue
from eie.expectations.units import Quantity
from eie.nlp.checks import checked
from eie.nlp.schema import EntityMentionKind, FieldName, MetricQualifier, NumericBound
from eie.nlp.spans import EvidenceSpan


@dataclass(frozen=True, slots=True)
class NumericMention:
    span: EvidenceSpan
    quantity: Quantity
    upper: Quantity | None = None
    bound: NumericBound = NumericBound.POINT
    field: FieldName = FieldName.AMOUNT
    qualifiers: tuple[MetricQualifier, ...] = ()
    ambiguous: bool = False

    def __post_init__(self) -> None:
        checked(self)
        if self.qualifiers != tuple(sorted(set(self.qualifiers))):
            raise ValidationError("distinct canonical metric qualifiers required")
        if (self.bound is NumericBound.CLOSED_RANGE) != (self.upper is not None):
            raise ValidationError("range semantics and upper value disagree")
        if self.upper is not None and (
            self.upper.canonical_unit != self.quantity.canonical_unit
            or self.upper.canonical_value < self.quantity.canonical_value
        ):
            raise ValidationError("compatible ordered extracted range required")

    @property
    def mention_id(self) -> str:
        return "nmention_" + fingerprint("eie:numeric-mention:v1", self)


@dataclass(frozen=True, slots=True)
class TemporalMention:
    span: EvidenceSpan
    value: TimeValue
    reason: str

    def __post_init__(self) -> None:
        checked(self)

    @property
    def mention_id(self) -> str:
        return "tmention_" + fingerprint("eie:temporal-mention:v1", self)


@dataclass(frozen=True, slots=True)
class PeriodMention:
    span: EvidenceSpan
    period: EarningsPeriod

    def __post_init__(self) -> None:
        checked(self)

    @property
    def mention_id(self) -> str:
        return "pmention_" + fingerprint("eie:period-mention:v1", self)


@dataclass(frozen=True, slots=True)
class EntityMention:
    span: EvidenceSpan
    kind: EntityMentionKind
    identifier: str
    candidate_ids: tuple[str, ...]
    state: ResolutionState
    method: str
    authority_ids: tuple[str, ...] = ()
    role: str | None = None

    def __post_init__(self) -> None:
        checked(self)
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise ValidationError("ordered distinct entity candidates required")
        if self.state is ResolutionState.RESOLVED and len(self.candidate_ids) != 1:
            raise ValidationError("one exact resolved entity required")
        if (
            self.kind in (EntityMentionKind.NAME, EntityMentionKind.PERSON_ROLE)
            and self.state is ResolutionState.RESOLVED
        ):
            raise ValidationError("name/person mention cannot silently resolve issuer")

    @property
    def mention_id(self) -> str:
        return "emention_" + fingerprint("eie:entity-mention:v1", self)
