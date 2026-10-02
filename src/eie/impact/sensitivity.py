"""Preserve model disagreement and contamination variants without ranking models."""

from dataclasses import dataclass, replace
from itertools import combinations
from math import isfinite

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.impact.engine import calculate
from eie.impact.models import ContaminationPolicy, EventImpactResult
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class SensitivityResult:
    left_impact_id: str
    right_impact_id: str
    car_label: str
    left_value: float | None
    right_value: float | None
    difference_right_minus_left: float | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        if self.left_impact_id >= self.right_impact_id:
            raise ValidationError("sensitivity pairs must be distinct and ordered")


@dataclass(frozen=True, slots=True)
class EventImpactSet:
    results: tuple[EventImpactResult, ...]
    sensitivity: tuple[SensitivityResult, ...]

    def __post_init__(self) -> None:
        contract(self)
        if not 2 <= len(self.results) <= 100 or tuple(r.impact_id for r in self.results) != tuple(
            sorted({r.impact_id for r in self.results})
        ):
            raise ValidationError("bounded sorted distinct impact set required")
        if (
            len(
                {
                    (
                        r.model_spec.study_id,
                        r.model_spec.study_fingerprint,
                        r.model_spec.semantics.timeframe,
                        r.model_spec.semantics.adjustment,
                        r.model_spec.semantics.return_type,
                        r.model_spec.semantics.method,
                    )
                    for r in self.results
                }
            )
            != 1
        ):
            raise ValidationError("sensitivity requires the same raw study and return semantics")

    @property
    def set_id(self) -> str:
        return "impact_set_" + fingerprint("eie:impact-set:v1", self)


def impact_set(results: tuple[EventImpactResult, ...]) -> EventImpactSet:
    rows = tuple(sorted(results, key=lambda r: r.impact_id))
    comparisons = []
    for left, right in combinations(rows, 2):
        for a in left.cars:
            for b in right.cars:
                if a.window != b.window:
                    continue
                difference = None if a.value is None or b.value is None else b.value - a.value
                reasons = set()
                if difference is None:
                    reasons.add("UNAVAILABLE_COMPARISON")
                elif not isfinite(difference):
                    difference = None
                    reasons.add("NUMERIC_UNAVAILABLE")
                if (
                    left.model_spec.contamination_policy
                    is not right.model_spec.contamination_policy
                ):
                    reasons.add("CONTAMINATION_POLICY_COMPARISON")
                comparisons.append(
                    SensitivityResult(
                        left.impact_id,
                        right.impact_id,
                        a.window.label,
                        a.value,
                        b.value,
                        difference,
                        tuple(sorted(reasons)),
                    )
                )
    return EventImpactSet(rows, tuple(comparisons))


def contamination_sensitivity(result: EventImpactResult) -> EventImpactSet:
    return impact_set(
        tuple(
            calculate(result.inputs, replace(result.model_spec, contamination_policy=p))
            for p in ContaminationPolicy
        )
    )
