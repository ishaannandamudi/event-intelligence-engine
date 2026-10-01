"""Validated offline extract records; no live vendor schema claims."""

from dataclasses import dataclass
from enum import StrEnum

from eie.expectations.models import MeasurementContext, Period
from eie.expectations.observations import ActualRole, EstimateKind
from eie.expectations.units import Quantity


class EstimateCapability(StrEnum):
    CONSENSUS = "CONSENSUS"
    ANALYST = "ANALYST"
    ACTUAL = "ACTUAL"
    GUIDANCE = "GUIDANCE"
    VINTAGES = "VINTAGES"


class RecordKind(StrEnum):
    ESTIMATE = "ESTIMATE"
    ACTUAL = "ACTUAL"
    GUIDANCE = "GUIDANCE"


@dataclass(frozen=True, slots=True)
class EstimateRecord:
    record_id: str
    kind: RecordKind
    context: MeasurementContext
    vintage_at_ns: int | None
    value: Quantity | None = None
    estimate_type: EstimateKind | None = None
    contributor_id: str | None = None
    contributor_count: int | None = None
    role: ActualRole | None = None
    published_at_ns: int | None = None
    previous_reference_period: Period | None = None
    issuer_id: str | None = None
    low: Quantity | None = None
    high: Quantity | None = None
    point: Quantity | None = None


@dataclass(frozen=True, slots=True)
class EstimateItem:
    item_id: str
    item_version: str
    published_at_ns: int | None
    source_reference: str
    records: tuple[EstimateRecord, ...]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class EstimateBatch:
    provider_id: str
    source_version: str
    items: tuple[EstimateItem, ...]
    coverage_event_ids: tuple[str, ...]
    coverage_measurements: tuple[str, ...]
    coverage_has_vintages: bool | None
