"""Extensible hierarchical event taxonomy."""

import re
from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError


class EventFamily(StrEnum):
    MACRO = "MACRO"
    CENTRAL_BANK = "CENTRAL_BANK"
    EARNINGS = "EARNINGS"
    SEC_FILING = "SEC_FILING"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    MANAGEMENT_GOVERNANCE = "MANAGEMENT_GOVERNANCE"
    PRODUCT_BUSINESS = "PRODUCT_BUSINESS"
    REGULATORY_LEGAL = "REGULATORY_LEGAL"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    OTHER = "OTHER"


PREFIX = {
    EventFamily.SEC_FILING: "SEC",
    EventFamily.MANAGEMENT_GOVERNANCE: "MANAGEMENT",
    EventFamily.PRODUCT_BUSINESS: "PRODUCT",
}
PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Z0-9][A-Z0-9_]*)+$")


@dataclass(frozen=True, slots=True)
class EventType:
    family: EventFamily
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.family, EventFamily) or not isinstance(self.code, str):
            raise ValidationError("typed event family and code required")
        expected = PREFIX.get(self.family, self.family.value)
        if not PATTERN.fullmatch(self.code) or self.code.split(".")[0] != expected:
            raise ValidationError("event type must be hierarchical and match its family")


MACRO_CPI_US = EventType(EventFamily.MACRO, "MACRO.CPI.US")
MACRO_CORE_CPI_US = EventType(EventFamily.MACRO, "MACRO.CORE_CPI.US")
MACRO_NFP_US = EventType(EventFamily.MACRO, "MACRO.NFP.US")
MACRO_CORE_PCE_US = EventType(EventFamily.MACRO, "MACRO.CORE_PCE.US")
FOMC_DECISION_US = EventType(EventFamily.CENTRAL_BANK, "CENTRAL_BANK.FOMC_DECISION.US")
FOMC_STATEMENT_US = EventType(EventFamily.CENTRAL_BANK, "CENTRAL_BANK.FOMC_STATEMENT.US")
FOMC_SEP_US = EventType(EventFamily.CENTRAL_BANK, "CENTRAL_BANK.FOMC_SEP.US")
FOMC_PRESS_CONFERENCE_US = EventType(
    EventFamily.CENTRAL_BANK, "CENTRAL_BANK.FOMC_PRESS_CONFERENCE.US"
)
FOMC_MINUTES_US = EventType(EventFamily.CENTRAL_BANK, "CENTRAL_BANK.FOMC_MINUTES.US")

REPRESENTATIVE_TYPES = tuple(
    [
        MACRO_CPI_US,
        MACRO_CORE_CPI_US,
        MACRO_NFP_US,
        MACRO_CORE_PCE_US,
        FOMC_DECISION_US,
        FOMC_STATEMENT_US,
        FOMC_SEP_US,
        FOMC_PRESS_CONFERENCE_US,
        FOMC_MINUTES_US,
    ]
    + [
        EventType(EventFamily.SEC_FILING, f"SEC.{name}")
        for name in ("8_K", "10_Q", "10_K", "6_K", "20_F", "S_1", "424B", "13D", "13G", "FORM_4")
    ]
    + [
        EventType(EventFamily.CORPORATE_ACTION, f"CORPORATE_ACTION.{name}")
        for name in (
            "DIVIDEND",
            "STOCK_SPLIT",
            "REVERSE_SPLIT",
            "BUYBACK",
            "TENDER_OFFER",
            "MERGER",
            "ACQUISITION",
            "SPIN_OFF",
            "DELISTING",
            "SYMBOL_CHANGE",
        )
    ]
    + [
        EventType(EventFamily.MANAGEMENT_GOVERNANCE, f"MANAGEMENT.{name}")
        for name in (
            "CEO_DEPARTURE",
            "CEO_APPOINTMENT",
            "CFO_DEPARTURE",
            "BOARD_CHANGE",
            "AUDITOR_CHANGE",
        )
    ]
    + [
        EventType(EventFamily.PRODUCT_BUSINESS, f"PRODUCT.{name}")
        for name in (
            "PRODUCT_LAUNCH",
            "PRODUCT_DELAY",
            "CONTRACT_WIN",
            "CONTRACT_LOSS",
            "REGULATORY_APPROVAL",
            "REGULATORY_REJECTION",
            "RECALL",
            "PARTNERSHIP",
        )
    ]
    + [
        EventType(EventFamily.MARKET_STRUCTURE, f"MARKET_STRUCTURE.{name}")
        for name in ("TRADING_HALT", "REOPENING", "EXCHANGE_DISRUPTION", "SUSPENSION")
    ]
    + [
        EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE"),
        EventType(EventFamily.REGULATORY_LEGAL, "REGULATORY_LEGAL.INVESTIGATION"),
        EventType(EventFamily.OTHER, "OTHER.UNCLASSIFIED"),
    ]
)
