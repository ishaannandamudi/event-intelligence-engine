"""Shared corporate identifiers, enums, periods, and exact money values."""

import re
from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.entities.models import ISSUER_ID
from eie.providers.sec.models import iso_date

SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
PROVIDER_ID = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")


class CorporateKind(StrEnum):
    EARNINGS_SCHEDULE = "EARNINGS_SCHEDULE"
    EARNINGS_RELEASE = "EARNINGS_RELEASE"
    EARNINGS_CALL = "EARNINGS_CALL"
    DIVIDEND = "DIVIDEND"
    SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
    STOCK_SPLIT = "STOCK_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    BUYBACK = "BUYBACK"
    TENDER_OFFER = "TENDER_OFFER"
    MERGER = "MERGER"
    ACQUISITION = "ACQUISITION"
    SPIN_OFF = "SPIN_OFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"
    CEO_DEPARTURE = "CEO_DEPARTURE"
    CEO_APPOINTMENT = "CEO_APPOINTMENT"
    CFO_DEPARTURE = "CFO_DEPARTURE"
    CFO_APPOINTMENT = "CFO_APPOINTMENT"
    BOARD_CHANGE = "BOARD_CHANGE"
    AUDITOR_CHANGE = "AUDITOR_CHANGE"


class ReleaseTiming(StrEnum):
    BEFORE_MARKET = "BEFORE_MARKET"
    DURING_MARKET = "DURING_MARKET"
    AFTER_MARKET = "AFTER_MARKET"
    UNKNOWN = "UNKNOWN"


class SchedulePrecision(StrEnum):
    EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
    DATE_PLUS_SESSION = "DATE_PLUS_SESSION"
    DATE_ONLY = "DATE_ONLY"
    UNKNOWN = "UNKNOWN"


class CorporateStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    ANNOUNCED = "ANNOUNCED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"


def _issuer(value: str | None, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or ISSUER_ID.fullmatch(value) is None:
        raise ValidationError(f"{name} requires stable issuer ID")


def _date(value: str | None, name: str) -> None:
    if value is not None:
        iso_date(value, name)


def _positive(value: int | None, name: str, *, zero: bool = False) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < int(not zero):
        raise ValidationError(
            f"{name} requires nonnegative integer" if zero else f"{name} must be positive"
        )


@dataclass(frozen=True, slots=True)
class EarningsPeriod:
    fiscal_year: int
    fiscal_quarter: int | None
    period_end: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.fiscal_year, bool)
            or not isinstance(self.fiscal_year, int)
            or not 1900 <= self.fiscal_year <= 2200
        ):
            raise ValidationError("invalid fiscal year")
        if self.fiscal_quarter is not None and (
            isinstance(self.fiscal_quarter, bool) or self.fiscal_quarter not in (1, 2, 3, 4)
        ):
            raise ValidationError("invalid fiscal quarter")
        iso_date(self.period_end, "period_end")

    @property
    def key(self) -> str:
        return f"FY{self.fiscal_year}-Q{self.fiscal_quarter or 0}-{self.period_end}"


@dataclass(frozen=True, slots=True)
class Money:
    amount_micros: int
    currency: str
    per_share: bool = False

    def __post_init__(self) -> None:
        _positive(self.amount_micros, "amount_micros", zero=True)
        if not isinstance(self.currency, str) or not CURRENCY.fullmatch(self.currency):
            raise ValidationError("currency must be ISO-style three uppercase letters")
        if not isinstance(self.per_share, bool):
            raise ValidationError("per_share must be boolean")
