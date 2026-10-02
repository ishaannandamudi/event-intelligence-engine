"""Typed impact selection and access to retained estimates and diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eie.core.errors import ValidationError
from eie.impact.models import (
    AbnormalReturnPoint,
    CumulativeAbnormalReturn,
    EventImpactResult,
    ModelFitResult,
    ModelType,
)
from eie.impact.sensitivity import EventImpactSet

if TYPE_CHECKING:
    from eie.impact.repository import ImpactRepository


class ImpactQueries:
    def __init__(self, repository: ImpactRepository) -> None:
        self.repository = repository

    def impact(self, impact_id: str) -> EventImpactResult:
        r = self.repository.get(impact_id)
        if r is None:
            raise ValidationError("impact unavailable")
        return r

    def abnormal_curve(self, impact_id: str) -> tuple[AbnormalReturnPoint, ...]:
        return self.impact(impact_id).abnormal_curve

    def car(self, impact_id: str, label: str) -> CumulativeAbnormalReturn:
        rows = tuple(c for c in self.impact(impact_id).cars if c.window.label == label)
        if not rows:
            raise ValidationError("declared CAR window unavailable")
        return rows[0]

    def fit_diagnostics(self, impact_id: str) -> ModelFitResult:
        return self.impact(impact_id).fit

    def sensitivity(self, set_id: str) -> EventImpactSet:
        value = self.repository.sensitivity(set_id)
        if value is None:
            raise ValidationError("sensitivity set unavailable")
        return value

    def impacts_as_of(
        self,
        as_of_ns: int,
        build_id: str,
        *,
        event_id: str | None = None,
        instrument_id: str | None = None,
        model_type: ModelType | None = None,
        exclude_contaminated: bool = False,
    ) -> tuple[EventImpactResult, ...]:
        return tuple(
            r
            for r in self.repository.impacts_as_of(
                as_of_ns,
                build_id=build_id,
                event_id=event_id,
                instrument_id=instrument_id,
                model_type=None if model_type is None else model_type.value,
            )
            if not exclude_contaminated or not r.inputs.study.contamination
        )
