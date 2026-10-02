"""Pure recalculation plus optional point-in-time historical study verification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from eie.core.errors import EIEError
from eie.impact.models import EventImpactResult
from eie.impact.sensitivity import EventImpactSet, impact_set
from eie.studies.checks import contract
from eie.studies.validation import StudyValidationStatus, validate_study

if TYPE_CHECKING:
    from eie.datasets.queries import DatasetReader


class ImpactValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ImpactValidationResult:
    status: ImpactValidationStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)


def validate_impact(
    result: EventImpactResult, events: DatasetReader | None = None
) -> ImpactValidationResult:
    from eie.impact.engine import calculate

    reasons = set()
    try:
        if calculate(result.inputs, result.model_spec) != result:
            reasons.add("CALCULATION_OR_METADATA_MISMATCH")
        raw = validate_study(result.inputs.study, events)
        if raw.status is StudyValidationStatus.INVALID:
            reasons.update(raw.reasons)
    except (EIEError, ValueError, KeyError, ArithmeticError):
        reasons.add("INVALID_IMPACT_CONTRACT")
    return ImpactValidationResult(
        ImpactValidationStatus.INVALID
        if reasons
        else ImpactValidationStatus.VALID_WITH_WARNINGS
        if result.quality
        else ImpactValidationStatus.VALID,
        tuple(sorted(reasons)) if reasons else tuple(sorted(q.value for q in result.quality)),
    )


def validate_set(value: EventImpactSet, events: DatasetReader | None = None) -> bool:
    return (
        all(
            validate_impact(r, events).status is not ImpactValidationStatus.INVALID
            for r in value.results
        )
        and impact_set(value.results) == value
    )
