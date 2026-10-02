"""Read-only validation by deterministic recalculation and optional build verification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from eie.core.errors import EIEError

if TYPE_CHECKING:
    from eie.datasets.queries import DatasetReader
from eie.studies.checks import contract
from eie.studies.models import AnchorQuality, EventStudyResult


class StudyValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class StudyValidationResult:
    status: StudyValidationStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)


def validate_study(
    result: EventStudyResult, events: DatasetReader | None = None
) -> StudyValidationResult:
    from eie.studies.engine import StudyEngine, calculate

    errors = set()
    try:
        expected = calculate(
            result.definition, result.market, result.benchmarks, result.contamination
        )
        if expected != result:
            errors.add("CALCULATION_OR_METADATA_MISMATCH")
        if events is not None:
            if (
                events.build_id != result.definition.event_build_id
                or events.manifest.content_fingerprint
                != result.definition.event_dataset_fingerprint
            ):
                errors.add("EVENT_DATASET_MISMATCH")
            else:
                from eie.studies.market import IndexedMarketProvider

                engine = StudyEngine(
                    events,
                    IndexedMarketProvider((result.market,)),
                    result.definition.selection_as_of_ns,
                )
                event = engine._events.get(result.definition.event.event_id)
                if not any(
                    f.fact_id == result.definition.historical_fact_id
                    and f.quality is result.definition.reconstruction_quality
                    for f in engine._facts.get(result.definition.event.event_id, [])
                ):
                    errors.add("HISTORICAL_FACT_REFERENCE_MISMATCH")
                if event != result.definition.event:
                    errors.add("EVENT_REVISION_MISMATCH")
                if (
                    result.definition.anchor.quality is AnchorQuality.EXACT_TIMESTAMP
                    and engine.anchor(
                        result.definition.event.event_id, result.definition.anchor.semantics
                    )
                    != result.definition.anchor
                ):
                    errors.add("ANCHOR_EVIDENCE_MISMATCH")
                expected_flags = engine.contamination.flags(
                    result.definition.event,
                    result.anchor_ns,
                    max(0, result.anchor_ns - result.definition.policy.contamination_pre_ns),
                    result.anchor_ns + result.definition.policy.contamination_post_ns,
                )
                if expected_flags != result.contamination:
                    errors.add("CONTAMINATION_HISTORY_MISMATCH")
    except (EIEError, ValueError, KeyError, ArithmeticError):
        errors.add("INVALID_STUDY_CONTRACT")
    return StudyValidationResult(
        StudyValidationStatus.INVALID
        if errors
        else StudyValidationStatus.VALID_WITH_WARNINGS
        if result.quality
        else StudyValidationStatus.VALID,
        tuple(sorted(errors)) if errors else tuple(sorted(q.value for q in result.quality)),
    )
