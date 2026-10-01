"""Reported units and safe canonical conversions; rates use fractional values."""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite

from eie.core.errors import ValidationError

Number = int | float


def number(value: Number, name: str = "value") -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} requires a number")
    try:
        valid = isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValidationError(f"{name} must be finite")


class UnitKind(StrEnum):
    CURRENCY = "CURRENCY"
    CURRENCY_PER_SHARE = "CURRENCY_PER_SHARE"
    COUNT = "COUNT"
    PERCENT = "PERCENT"
    PERCENTAGE_POINTS = "PERCENTAGE_POINTS"
    BASIS_POINTS = "BASIS_POINTS"
    INDEX_LEVEL = "INDEX_LEVEL"
    ANNUALIZED_GROWTH = "ANNUALIZED_GROWTH"
    RATIO = "RATIO"


class Magnitude(StrEnum):
    ONE = "ONE"
    THOUSAND = "THOUSAND"
    MILLION = "MILLION"
    BILLION = "BILLION"


class Dimension(StrEnum):
    CURRENCY = "CURRENCY"
    CURRENCY_PER_SHARE = "CURRENCY_PER_SHARE"
    COUNT = "COUNT"
    RATE = "RATE"
    RATE_CHANGE = "RATE_CHANGE"
    INDEX_LEVEL = "INDEX_LEVEL"
    ANNUALIZED_RATE = "ANNUALIZED_RATE"
    RATIO = "RATIO"


@dataclass(frozen=True, slots=True)
class CanonicalUnit:
    dimension: Dimension
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, Dimension):
            raise ValidationError("typed canonical dimension required")
        money = self.dimension in (Dimension.CURRENCY, Dimension.CURRENCY_PER_SHARE)
        if money != (self.currency is not None) or (
            self.currency is not None and re.fullmatch(r"[A-Z]{3}", self.currency) is None
        ):
            raise ValidationError("currency inconsistent with canonical dimension")


@dataclass(frozen=True, slots=True)
class Unit:
    kind: UnitKind
    magnitude: Magnitude = Magnitude.ONE
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnitKind) or not isinstance(self.magnitude, Magnitude):
            raise ValidationError("typed unit and magnitude required")
        money = self.kind in (UnitKind.CURRENCY, UnitKind.CURRENCY_PER_SHARE)
        if money != (self.currency is not None) or (
            self.currency is not None and re.fullmatch(r"[A-Z]{3}", self.currency) is None
        ):
            raise ValidationError("currency inconsistent with reported unit")
        if self.magnitude is not Magnitude.ONE and self.kind not in (
            UnitKind.CURRENCY,
            UnitKind.CURRENCY_PER_SHARE,
            UnitKind.COUNT,
        ):
            raise ValidationError("scaled magnitude only supports money and counts")

    @property
    def canonical(self) -> CanonicalUnit:
        dimension = {
            UnitKind.PERCENT: Dimension.RATE,
            UnitKind.PERCENTAGE_POINTS: Dimension.RATE_CHANGE,
            UnitKind.BASIS_POINTS: Dimension.RATE_CHANGE,
            UnitKind.ANNUALIZED_GROWTH: Dimension.ANNUALIZED_RATE,
        }.get(self.kind)
        return CanonicalUnit(dimension or Dimension(self.kind.value), self.currency)

    @property
    def factor(self) -> Number:
        if self.kind in (UnitKind.PERCENT, UnitKind.PERCENTAGE_POINTS, UnitKind.ANNUALIZED_GROWTH):
            return 0.01
        if self.kind is UnitKind.BASIS_POINTS:
            return 0.0001
        return {
            Magnitude.ONE: 1,
            Magnitude.THOUSAND: 1000,
            Magnitude.MILLION: 1_000_000,
            Magnitude.BILLION: 1_000_000_000,
        }[self.magnitude]


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Number
    unit: Unit
    canonical_value: Number = field(init=False)
    canonical_unit: CanonicalUnit = field(init=False)

    def __post_init__(self) -> None:
        number(self.value)
        if not isinstance(self.unit, Unit):
            raise ValidationError("typed reported unit required")
        try:
            normalized = self.value * self.unit.factor
        except OverflowError as exc:
            raise ValidationError("unit conversion overflow") from exc
        number(normalized, "canonical value")
        object.__setattr__(self, "canonical_value", normalized)
        object.__setattr__(self, "canonical_unit", self.unit.canonical)
