"""Future resolution interface; no fuzzy matching in EIE-0."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from eie.core.errors import ValidationError
from eie.core.quality import QualityFlag


class ResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    state: ResolutionState
    candidate_ids: tuple[str, ...]
    method: str
    reason_codes: tuple[str, ...] = ()
    quality_flags: frozenset[QualityFlag] = frozenset()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.state, ResolutionState)
            or not isinstance(self.candidate_ids, tuple)
            or not isinstance(self.reason_codes, tuple)
            or not isinstance(self.quality_flags, frozenset)
            or any(not isinstance(item, str) for item in self.candidate_ids)
            or any(not isinstance(item, str) for item in self.reason_codes)
            or any(not isinstance(item, QualityFlag) for item in self.quality_flags)
        ):
            raise ValidationError("typed immutable resolution fields required")
        if not self.method or tuple(sorted(set(self.candidate_ids))) != self.candidate_ids:
            raise ValidationError("resolution method and sorted unique candidates required")
        if self.state is ResolutionState.RESOLVED and len(self.candidate_ids) != 1:
            raise ValidationError("RESOLVED requires exactly one candidate")
        if self.state is ResolutionState.UNRESOLVED and self.candidate_ids:
            raise ValidationError("UNRESOLVED cannot have candidates")
        if (
            self.state in (ResolutionState.AMBIGUOUS, ResolutionState.CONFLICTING)
            and len(self.candidate_ids) < 2
        ):
            raise ValidationError("ambiguous/conflicting requires multiple candidates")


class EntityResolver(Protocol):
    def resolve(
        self, identifier: str, as_of_ns: int, valid_at_ns: int | None = None
    ) -> ResolutionResult: ...
