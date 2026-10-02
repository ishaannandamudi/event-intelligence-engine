"""Policies and resolutions pin references rather than duplicating evidence bodies."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import ClaimValue, ordered
from eie.evidence.schema import ConflictType, EvidenceQuality, ResolutionState
from eie.sources.trust import SourceClass, SourcePrecedencePolicy
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class EvidenceLimits:
    claims_per_item: int = 1000
    references_per_item: int = 4096
    relationships_per_claim: int = 64
    candidates_per_event: int = 128
    group_members: int = 256
    query_size: int = 10000
    traversal_depth: int = 8

    def __post_init__(self) -> None:
        contract(self)
        if not all(
            1 <= v <= maximum
            for v, maximum in (
                (self.claims_per_item, 10000),
                (self.references_per_item, 10000),
                (self.relationships_per_claim, 256),
                (self.candidates_per_event, 1000),
                (self.group_members, 1000),
                (self.query_size, 100000),
                (self.traversal_depth, 8),
            )
        ):
            raise ValidationError("bounded evidence limits required")


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    precedence: SourcePrecedencePolicy = SourcePrecedencePolicy()
    prefer_exact_time: bool = True

    def __post_init__(self) -> None:
        contract(self)

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:claim-resolution-policy:v1", self)


@dataclass(frozen=True, slots=True)
class ClaimConflict:
    left_id: str
    right_id: str
    kind: ConflictType

    def __post_init__(self) -> None:
        contract(self)
        if self.left_id >= self.right_id:
            raise ValidationError("canonical distinct conflict pair required")


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    independent_source_count: int
    source_classes: frozenset[SourceClass]
    confirmation_count: int
    contradiction_count: int
    correction_count: int
    retraction_count: int
    unresolved_conflicts: int
    latest_confirmation_ns: int | None = None
    latest_contradiction_ns: int | None = None

    def __post_init__(self) -> None:
        contract(self)
        if (
            min(
                self.independent_source_count,
                self.confirmation_count,
                self.contradiction_count,
                self.correction_count,
                self.retraction_count,
                self.unresolved_conflicts,
            )
            < 0
        ):
            raise ValidationError("nonnegative descriptive evidence counts required")


@dataclass(frozen=True, slots=True)
class CanonicalClaimResolution:
    claim_id: str
    dataset_id: str
    known_as_of_ns: int
    valid_at_ns: int | None
    state: ResolutionState
    selected_id: str | None
    value: ClaimValue | None
    alternative_ids: tuple[str, ...]
    active_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    conflicts: tuple[ClaimConflict, ...]
    reasons: tuple[str, ...]
    quality: frozenset[EvidenceQuality]
    summary: EvidenceSummary
    policy: ResolutionPolicy

    def __post_init__(self) -> None:
        contract(self)
        for values in (
            self.alternative_ids,
            self.active_ids,
            self.evidence_ids,
            self.relationship_ids,
            self.reasons,
        ):
            ordered(values, 100000)
        if not set(self.active_ids) <= set(self.alternative_ids) or (
            self.selected_id is not None and self.selected_id not in self.active_ids
        ):
            raise ValidationError("resolution membership disagrees")
        if (self.state is ResolutionState.RESOLVED) != (
            self.selected_id is not None and self.value is not None
        ):
            raise ValidationError("resolved claim requires selected sourced value")
        if self.state is not ResolutionState.RESOLVED and (
            self.selected_id is not None or self.value is not None
        ):
            raise ValidationError("unresolved/absent/retracted claims cannot invent a value")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:canonical-claim-resolution:v1", self)

    @property
    def resolution_id(self) -> str:
        return "cres_" + self.result_fingerprint


DEFAULT_EVIDENCE_LIMITS = EvidenceLimits()
DEFAULT_RESOLUTION_POLICY = ResolutionPolicy()
