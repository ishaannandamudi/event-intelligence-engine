"""Categorical trust and future precedence foundation."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError


class SourceClass(StrEnum):
    OFFICIAL_PRIMARY = "OFFICIAL_PRIMARY"
    EXCHANGE_PRIMARY = "EXCHANGE_PRIMARY"
    COMPANY_PRIMARY = "COMPANY_PRIMARY"
    REGULATED_FILING = "REGULATED_FILING"
    LICENSED_NEWS = "LICENSED_NEWS"
    SECONDARY_NEWS = "SECONDARY_NEWS"
    UNVERIFIED = "UNVERIFIED"


DEFAULT_PRECEDENCE = (
    SourceClass.OFFICIAL_PRIMARY,
    SourceClass.EXCHANGE_PRIMARY,
    SourceClass.COMPANY_PRIMARY,
    SourceClass.REGULATED_FILING,
    SourceClass.LICENSED_NEWS,
    SourceClass.SECONDARY_NEWS,
    SourceClass.UNVERIFIED,
)


@dataclass(frozen=True, slots=True)
class SourcePrecedencePolicy:
    """Baseline ordering; later fact-specific rules may override it."""

    baseline: tuple[SourceClass, ...] = DEFAULT_PRECEDENCE
    fact_overrides: tuple[tuple[str, tuple[SourceClass, ...]], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.baseline, tuple)
            or len(self.baseline) != len(SourceClass)
            or any(not isinstance(item, SourceClass) for item in self.baseline)
            or set(self.baseline) != set(SourceClass)
        ):
            raise ValidationError("baseline must contain every source class once")
        if not isinstance(self.fact_overrides, tuple):
            raise ValidationError("fact overrides must be immutable")
        names = tuple(name for name, _ in self.fact_overrides)
        if names != tuple(sorted(set(names))) or any(not name for name in names):
            raise ValidationError("fact override names must be sorted, unique and nonempty")
        if any(
            not isinstance(order, tuple)
            or len(order) != len(SourceClass)
            or any(not isinstance(item, SourceClass) for item in order)
            or set(order) != set(SourceClass)
            for _, order in self.fact_overrides
        ):
            raise ValidationError("fact override must order every source class once")

    def order_for(self, fact_type: str) -> tuple[SourceClass, ...]:
        return next(
            (order for name, order in self.fact_overrides if name == fact_type), self.baseline
        )
