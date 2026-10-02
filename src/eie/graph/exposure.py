"""Explicit fractions/ranges; derived path ownership is never a sourced fact."""

from dataclasses import dataclass
from enum import StrEnum
from math import prod

from eie.core.errors import ValidationError
from eie.studies.checks import contract


class ExposureType(StrEnum):
    PORTFOLIO_WEIGHT = "PORTFOLIO_WEIGHT"
    OWNERSHIP_PERCENT = "OWNERSHIP_PERCENT"
    REVENUE_SHARE = "REVENUE_SHARE"
    PURCHASE_SHARE = "PURCHASE_SHARE"
    INDEX_WEIGHT = "INDEX_WEIGHT"
    VOTING_POWER = "VOTING_POWER"
    ECONOMIC_INTEREST = "ECONOMIC_INTEREST"


class ExposureUnit(StrEnum):
    FRACTION = "FRACTION"
    PERCENT = "PERCENT"


class ExposureDomain(StrEnum):
    UNIT_INTERVAL = "UNIT_INTERVAL"
    SIGNED_PORTFOLIO = "SIGNED_PORTFOLIO"


@dataclass(frozen=True, slots=True)
class ExposureMeasurement:
    measurement_type: ExposureType
    reference_ns: int
    source_reference: str
    point: float | None = None
    lower: float | None = None
    upper: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    unit: ExposureUnit = ExposureUnit.FRACTION
    domain: ExposureDomain = ExposureDomain.UNIT_INTERVAL
    estimated: bool = False

    def __post_init__(self) -> None:
        contract(self)
        if self.unit is not ExposureUnit.FRACTION:
            raise ValidationError(
                "canonical exposures require fractions; normalize percent explicitly"
            )
        if self.point is not None and (self.lower is not None or self.upper is not None):
            raise ValidationError("exposure is an exact point or bounds")
        if self.point is None and self.lower is None and self.upper is None:
            raise ValidationError("unknown exposure is None, not an empty measurement")
        if self.domain is ExposureDomain.SIGNED_PORTFOLIO:
            if self.measurement_type is not ExposureType.PORTFOLIO_WEIGHT:
                raise ValidationError(
                    "signed domain only describes explicitly supplied portfolio weights"
                )
            low, high = -100.0, 100.0
        else:
            low, high = 0.0, 1.0
        if any(
            v is not None and not low <= v <= high for v in (self.point, self.lower, self.upper)
        ):
            raise ValidationError("exposure outside declared range")
        if (
            self.lower is not None
            and self.upper is not None
            and (
                self.lower > self.upper
                or self.lower == self.upper
                and not (self.lower_inclusive and self.upper_inclusive)
            )
        ):
            raise ValidationError("empty exposure range")


def exposure_from_percent(
    kind: ExposureType, value: float, reference_ns: int, source_reference: str
) -> ExposureMeasurement:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("numeric percent required")
    return ExposureMeasurement(kind, reference_ns, source_reference, point=value / 100)


@dataclass(frozen=True, slots=True)
class HoldingQuantity:
    reference_ns: int
    shares: float | None = None
    market_value: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        if self.shares is None and self.market_value is None:
            raise ValidationError("supplied holding quantity required")
        if any(v is not None and v < 0 for v in (self.shares, self.market_value)):
            raise ValidationError("holding quantity must be nonnegative")
        if (self.market_value is None) != (self.currency is None) or (
            self.currency is not None
            and (
                len(self.currency) != 3
                or not self.currency.isascii()
                or not self.currency.isalpha()
                or not self.currency.isupper()
            )
        ):
            raise ValidationError("market value requires an explicit three-letter currency")


@dataclass(frozen=True, slots=True)
class DerivedPathExposure:
    measurement_type: ExposureType
    point: float | None
    lower: float | None
    upper: float | None
    lower_inclusive: bool
    upper_inclusive: bool
    input_fingerprints: tuple[str, ...]
    method: str = "DERIVED_PATH_EXPOSURE_OWNERSHIP_PRODUCT"

    def __post_init__(self) -> None:
        contract(self)


def compose_ownership(
    values: tuple[ExposureMeasurement | None, ...], fingerprints: tuple[str, ...]
) -> DerivedPathExposure | None:
    if not values or len(values) != len(fingerprints):
        raise ValidationError("ordered exposures and edge fingerprints required")
    if any(v is None for v in values):
        return None
    rows = tuple(v for v in values if v is not None)
    if any(
        v.measurement_type is not ExposureType.OWNERSHIP_PERCENT
        or v.domain is not ExposureDomain.UNIT_INTERVAL
        for v in rows
    ):
        raise ValidationError("only compatible ownership fractions compose under this policy")
    exact = all(v.point is not None for v in rows)
    if any(v.point == 0 or v.point is None and v.upper == 0 for v in rows):
        return DerivedPathExposure(
            ExposureType.OWNERSHIP_PERCENT, 0.0, None, None, True, True, fingerprints
        )
    points = tuple(v.point for v in rows if v.point is not None)
    lower = prod(v.point if v.point is not None else v.lower or 0.0 for v in rows)
    upper = prod(
        v.point if v.point is not None else v.upper if v.upper is not None else 1.0 for v in rows
    )
    # Products that underflow cannot masquerade as precisely zero ownership.
    if upper == 0 and all((v.point if v.point is not None else v.upper) != 0 for v in rows):
        raise ValidationError("derived ownership underflow")
    lower_values = tuple(v.point if v.point is not None else v.lower or 0.0 for v in rows)
    if lower == 0 and all(v > 0 for v in lower_values):
        raise ValidationError("derived ownership lower-bound underflow")
    return DerivedPathExposure(
        ExposureType.OWNERSHIP_PERCENT,
        prod(points) if exact else None,
        None if exact else lower,
        None if exact else upper,
        any(v.point is None and v.lower == 0 and v.lower_inclusive for v in rows)
        or all(v.point is not None or v.lower is None or v.lower_inclusive for v in rows),
        all(v.point is not None or v.upper is None or v.upper_inclusive for v in rows),
        fingerprints,
    )
