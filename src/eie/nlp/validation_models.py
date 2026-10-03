"""Validation reports contain fixed categories and counts, never source bodies/errors."""

from dataclasses import dataclass
from enum import StrEnum

from eie.nlp.checks import checked


class ValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"


class ValidationReason(StrEnum):
    CONTENT_NOT_RETAINED = "CONTENT_NOT_RETAINED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class TextValidation:
    status: ValidationStatus
    reasons: tuple[ValidationReason, ...]
    counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        checked(self)
