"""Conservative source cues; no sentiment, materiality or automatic deal completion."""

import re

from eie.events.taxonomy import EventFamily, EventType
from eie.nlp.schema import EpistemicStatus, UnscheduledFamily

PATTERNS: tuple[tuple[UnscheduledFamily, str], ...] = (
    (
        UnscheduledFamily.EARNINGS_RESULT,
        r"\b(?:reported|reports|released|announced)\b.{0,80}\b(?:revenue|EPS|earnings|margin)\b",
    ),
    (UnscheduledFamily.GUIDANCE_CHANGE, r"\b(?:guidance|revenue forecast|EPS forecast)\b"),
    (
        UnscheduledFamily.MANAGEMENT_CHANGE,
        r"\b(?:CEO|CFO|director|auditor)\b.{0,80}?\b"
        r"(?:resign\w*|depart\w*|appoint\w*|step\w* down)\b",
    ),
    (
        UnscheduledFamily.M_AND_A,
        r"\b(?:acquir\w*|acquisition|merger|sale process|exploring a sale)\b",
    ),
    (
        UnscheduledFamily.CAPITAL_RAISE,
        r"\b(?:capital raise|raised capital|equity offering|debt offering)\b",
    ),
    (UnscheduledFamily.BUYBACK, r"\b(?:buyback|share repurchase)\b"),
    (UnscheduledFamily.DIVIDEND, r"\bdividend\b"),
    (
        UnscheduledFamily.LEGAL_REGULATORY,
        r"\b(?:investigation|lawsuit|court order|regulatory fine)\b",
    ),
    (UnscheduledFamily.CYBERSECURITY, r"\b(?:cyberattack|data breach|ransomware|cyber incident)\b"),
    (UnscheduledFamily.PRODUCT, r"\b(?:product launch|launched a product|product delay)\b"),
    (
        UnscheduledFamily.CONTRACT,
        r"\b(?:contract award|awarded a contract|contract loss|signed a contract)\b",
    ),
    (
        UnscheduledFamily.SUPPLY_CHAIN,
        r"\b(?:supplier interruption|supply agreement|supply disruption)\b",
    ),
    (
        UnscheduledFamily.OPERATIONAL_DISRUPTION,
        r"\b(?:factory shutdown|production delay|capacity expansion|operations halted)\b",
    ),
    (UnscheduledFamily.RECALL, r"\brecall\w*\b"),
    (UnscheduledFamily.BANKRUPTCY_RESTRUCTURING, r"\b(?:bankruptcy|restructuring|chapter 11)\b"),
    (UnscheduledFamily.ACTIVIST, r"\b(?:activist investor|proxy contest)\b"),
    (UnscheduledFamily.OWNERSHIP, r"\b(?:ownership stake|purchased a stake|sold a stake)\b"),
    (UnscheduledFamily.RATING_ACTION, r"\b(?:rating downgrade|rating upgrade|credit rating)\b"),
    (UnscheduledFamily.FDA_REGULATORY_DECISION, r"\bFDA\b.{0,80}\b(?:approv\w*|reject\w*)\b"),
    (UnscheduledFamily.OTHER, r"\bunscheduled announcement\b"),
)


def classify(text: str) -> tuple[UnscheduledFamily, ...]:
    return tuple(sorted(family for family, pattern in PATTERNS if re.search(pattern, text, re.I)))


def epistemic(text: str, family: UnscheduledFamily) -> EpistemicStatus:
    if re.search(r"\b(?:denies|denied|denial)\b", text, re.I):
        return EpistemicStatus.DENIED
    if re.search(r"\b(?:did not|has not|never|no resignation|not resign)\b", text, re.I):
        return EpistemicStatus.NEGATED
    if re.search(r"\b(?:rumor\w*|sources say|reportedly)\b", text, re.I):
        return EpistemicStatus.RUMORED
    if re.search(r"\b(?:may|could|if approved|subject to approval)\b", text, re.I):
        return EpistemicStatus.CONDITIONAL
    if re.search(r"\b(?:plans to|planned|intends to|will acquire|will appoint)\b", text, re.I):
        return EpistemicStatus.PLANNED
    if family is not UnscheduledFamily.GUIDANCE_CHANGE and re.search(
        r"\b(?:expects|expected|anticipates)\b", text, re.I
    ):
        return EpistemicStatus.EXPECTED
    if re.search(r"[\"“”]|\balleged\b", text, re.I):
        return EpistemicStatus.ALLEGED
    return EpistemicStatus.REPORTED


def event_type(family: UnscheduledFamily, text: str = "") -> EventType:
    if family is UnscheduledFamily.EARNINGS_RESULT:
        return EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE")
    if family is UnscheduledFamily.GUIDANCE_CHANGE:
        return EventType(EventFamily.EARNINGS, "EARNINGS.GUIDANCE")
    if family is UnscheduledFamily.MANAGEMENT_CHANGE:
        role = (
            "CFO"
            if re.search(r"\bCFO\b", text)
            else "CEO"
            if re.search(r"\bCEO\b", text)
            else "BOARD"
        )
        action = (
            "APPOINTMENT"
            if re.search(r"\bappoint", text, re.I)
            else "DEPARTURE"
            if role in ("CEO", "CFO")
            else "CHANGE"
        )
        return EventType(EventFamily.MANAGEMENT_GOVERNANCE, f"MANAGEMENT.{role}_{action}")
    if family in (
        UnscheduledFamily.M_AND_A,
        UnscheduledFamily.CAPITAL_RAISE,
        UnscheduledFamily.BUYBACK,
        UnscheduledFamily.DIVIDEND,
        UnscheduledFamily.OWNERSHIP,
    ):
        code = "ACQUISITION" if family is UnscheduledFamily.M_AND_A else family.value
        return EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION." + code)
    if family in (
        UnscheduledFamily.LEGAL_REGULATORY,
        UnscheduledFamily.FDA_REGULATORY_DECISION,
        UnscheduledFamily.RATING_ACTION,
    ):
        return EventType(EventFamily.REGULATORY_LEGAL, "REGULATORY_LEGAL." + family.value)
    if family is UnscheduledFamily.BANKRUPTCY_RESTRUCTURING:
        return EventType(EventFamily.MANAGEMENT_GOVERNANCE, "MANAGEMENT.BANKRUPTCY_RESTRUCTURING")
    if family is UnscheduledFamily.OTHER:
        return EventType(EventFamily.OTHER, "OTHER.UNCLASSIFIED")
    return EventType(EventFamily.PRODUCT_BUSINESS, "PRODUCT." + family.value)
