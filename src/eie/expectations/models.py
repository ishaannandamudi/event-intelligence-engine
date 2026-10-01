"""Stable measurement, reference-period, event-link, and provider identities."""

import re
from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.time import require_ns
from eie.corporate.types import EarningsPeriod
from eie.entities.models import ISSUER_ID, SECURITY_ID
from eie.events.taxonomy import EventType
from eie.providers.common.models import ReferencePeriod
from eie.sources.trust import SourceClass

Period = EarningsPeriod | ReferencePeriod


def text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{name} requires trimmed nonempty text")


def flags(value: frozenset[QualityFlag]) -> None:
    if not isinstance(value, frozenset) or any(not isinstance(v, QualityFlag) for v in value):
        raise ValidationError("typed immutable quality flags required")


@dataclass(frozen=True, slots=True)
class MeasurementId:
    code: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or re.fullmatch(
                r"(?:EARNINGS|MACRO|GUIDANCE)\.[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*", self.code
            )
            is None
        ):
            raise ValidationError("hierarchical earnings/macro/guidance measurement required")


EPS_DILUTED = MeasurementId("EARNINGS.EPS.DILUTED")
REVENUE = MeasurementId("EARNINGS.REVENUE")
GROSS_MARGIN = MeasurementId("EARNINGS.GROSS_MARGIN")
OPERATING_MARGIN = MeasurementId("EARNINGS.OPERATING_MARGIN")
GUIDANCE_EPS = MeasurementId("GUIDANCE.EPS")
GUIDANCE_REVENUE = MeasurementId("GUIDANCE.REVENUE")
CPI_HEADLINE_MOM = MeasurementId("MACRO.CPI.HEADLINE.MOM")
CPI_HEADLINE_YOY = MeasurementId("MACRO.CPI.HEADLINE.YOY")
CPI_CORE_MOM = MeasurementId("MACRO.CPI.CORE.MOM")
CPI_CORE_YOY = MeasurementId("MACRO.CPI.CORE.YOY")
NFP_PAYROLL_CHANGE = MeasurementId("MACRO.NFP.PAYROLL_CHANGE")
UNEMPLOYMENT_RATE = MeasurementId("MACRO.UNEMPLOYMENT_RATE")
WAGES_MOM = MeasurementId("MACRO.AVERAGE_HOURLY_EARNINGS.MOM")
PCE_CORE_MOM = MeasurementId("MACRO.PCE.CORE.MOM")
GDP_REAL_ANNUALIZED_QOQ = MeasurementId("MACRO.GDP.REAL.ANNUALIZED_QOQ")


class SeasonalAdjustment(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    ADJUSTED = "ADJUSTED"
    NOT_ADJUSTED = "NOT_ADJUSTED"


@dataclass(frozen=True, slots=True)
class MeasurementContext:
    event_id: str
    measurement_id: MeasurementId
    reference_period: Period
    release_stage: str | None = None
    seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNSPECIFIED

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_id, str)
            or re.fullmatch(r"evt_[0-9a-f]{64}", self.event_id) is None
        ):
            raise ValidationError("stable canonical event ID required")
        if (
            not isinstance(self.measurement_id, MeasurementId)
            or not isinstance(self.reference_period, (EarningsPeriod, ReferencePeriod))
            or not isinstance(self.seasonal_adjustment, SeasonalAdjustment)
        ):
            raise ValidationError("typed measurement context required")
        if self.release_stage is not None:
            text(self.release_stage, "release_stage")


@dataclass(frozen=True, slots=True)
class EventReference:
    event_id: str
    event_type: EventType
    reference_period: Period
    available_from_ns: int
    issuer_id: str | None = None
    security_id: str | None = None
    release_stage: str | None = None

    def __post_init__(self) -> None:
        MeasurementContext(self.event_id, EPS_DILUTED, self.reference_period, self.release_stage)
        require_ns(self.available_from_ns)
        if not isinstance(self.event_type, EventType):
            raise ValidationError("typed event type required")
        for value, pattern in ((self.issuer_id, ISSUER_ID), (self.security_id, SECURITY_ID)):
            if value is not None and (
                not isinstance(value, str) or pattern.fullmatch(value) is None
            ):
                raise ValidationError("stable issuer/security identity required")


@dataclass(frozen=True, slots=True)
class ObservationSource:
    provider_id: str
    provider_source_item_id: str
    source_observation_id: str
    source_class: SourceClass
    available_from_ns: int
    ingested_at_ns: int
    provider_observed_at_ns: int
    vintage_at_ns: int | None = None
    provider_record_id: str = "PRIMARY"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_id, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", self.provider_id) is None
        ):
            raise ValidationError("stable provider ID required")
        text(self.provider_source_item_id, "provider source item")
        text(self.provider_record_id, "provider record ID")
        if not isinstance(
            self.source_observation_id, str
        ) or not self.source_observation_id.startswith("obs_"):
            raise ValidationError("raw observation evidence required")
        if not isinstance(self.source_class, SourceClass):
            raise ValidationError("typed source class required")
        for value in (self.available_from_ns, self.ingested_at_ns, self.provider_observed_at_ns):
            require_ns(value)
        if not self.provider_observed_at_ns <= self.ingested_at_ns <= self.available_from_ns:
            raise TemporalConsistencyError("provider/ingestion/availability clocks reversed")
        if self.vintage_at_ns is not None:
            require_ns(self.vintage_at_ns)
            if self.vintage_at_ns > self.provider_observed_at_ns:
                raise TemporalConsistencyError("estimate vintage follows provider observation")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:expectation-source:v1", self)
