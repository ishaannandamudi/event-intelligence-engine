"""Semantic propositions and immutable sourced assertions have separate identities."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from eie.backfill.models import ProvenanceKind, ReconstructionQuality
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.corporate.models import CorporatePayload
from eie.corporate.types import CorporateStatus, ReleaseTiming
from eie.events.lifecycle import LifecycleState
from eie.evidence import CLAIM_EVIDENCE_SCHEMA_VERSION
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate, SubjectKind
from eie.expectations.models import MeasurementContext, Period
from eie.expectations.observations import ActualRole, EstimateKind
from eie.expectations.units import CanonicalUnit, Quantity
from eie.filings.models import FilingMetadata
from eie.graph.exposure import ExposureMeasurement
from eie.graph.taxonomy import RelationshipType
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract


def identity(value: str, prefixes: tuple[str, ...]) -> None:
    if re.fullmatch("(?:" + "|".join(prefixes) + ")_[0-9a-f]{64}", value) is None:
        raise ValidationError("stable typed evidence identity required")


def ordered(values: tuple[str, ...], maximum: int, *, empty: bool = True) -> None:
    if len(values) > maximum or values != tuple(sorted(set(values))) or not empty and not values:
        raise ValidationError("bounded sorted distinct evidence references required")


@dataclass(frozen=True, slots=True)
class ClaimSubject:
    kind: SubjectKind
    identity: str

    def __post_init__(self) -> None:
        contract(self)
        prefixes = {
            SubjectKind.EVENT: ("evt",),
            SubjectKind.ENTITY: ("iss", "sec", "lst", "node"),
            SubjectKind.FILING: ("fil", "evt"),
            SubjectKind.RELATIONSHIP: ("edge",),
            SubjectKind.CORPORATE: ("corp",),
            SubjectKind.MEASUREMENT: ("evt", "corp"),
        }
        identity(self.identity, prefixes[self.kind])


class TimePrecision(StrEnum):
    UNKNOWN = "UNKNOWN"
    DATE_ONLY = "DATE_ONLY"
    DATE_PLUS_SESSION = "DATE_PLUS_SESSION"
    EXACT = "EXACT"


@dataclass(frozen=True, slots=True)
class TimeValue:
    precision: TimePrecision
    date: str | None = None
    timestamp_ns: int | None = None
    session: ReleaseTiming = ReleaseTiming.UNKNOWN

    def __post_init__(self) -> None:
        contract(self)
        if self.date is not None:
            try:
                if date.fromisoformat(self.date).isoformat() != self.date:
                    raise ValueError
            except ValueError as exc:
                raise ValidationError("ISO calendar date required") from exc
        if self.precision is TimePrecision.UNKNOWN:
            if (
                self.date is not None
                or self.timestamp_ns is not None
                or self.session is not ReleaseTiming.UNKNOWN
            ):
                raise ValidationError("unknown time cannot fabricate timing")
        elif self.date is None:
            raise ValidationError("known time requires a date")
        if self.precision is TimePrecision.EXACT:
            if (
                self.timestamp_ns is None
                or (
                    datetime(1970, 1, 1, tzinfo=UTC)
                    + timedelta(seconds=self.timestamp_ns // 1_000_000_000)
                )
                .date()
                .isoformat()
                != self.date
            ):
                raise ValidationError("exact UTC time and date disagree")
        elif self.timestamp_ns is not None:
            raise ValidationError("coarse precision cannot carry an exact timestamp")
        if (
            self.precision is TimePrecision.DATE_PLUS_SESSION
            and self.session is ReleaseTiming.UNKNOWN
        ):
            raise ValidationError("session precision needs supplied session")
        if self.precision is TimePrecision.DATE_ONLY and self.session is not ReleaseTiming.UNKNOWN:
            raise ValidationError("date-only cannot imply session")


@dataclass(frozen=True, slots=True)
class StatusValue:
    status: LifecycleState | CorporateStatus

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class IdentityValue:
    target: ClaimSubject

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class OccurrenceValue:
    occurred: bool

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class NumericRange:
    lower: Quantity | None
    upper: Quantity | None
    point: Quantity | None = None

    def __post_init__(self) -> None:
        contract(self)
        supplied = tuple(v for v in (self.lower, self.upper, self.point) if v is not None)
        if not supplied or len({v.canonical_unit for v in supplied}) != 1:
            raise ValidationError("numeric range requires compatible supplied bounds/point")
        if (
            self.lower is not None
            and self.upper is not None
            and self.lower.canonical_value > self.upper.canonical_value
        ):
            raise ValidationError("ordered compatible numeric range required")
        if self.point is not None and (
            self.lower is not None
            and self.point.canonical_value < self.lower.canonical_value
            or self.upper is not None
            and self.point.canonical_value > self.upper.canonical_value
        ):
            raise ValidationError("numeric point lies outside range")

    @property
    def canonical_unit(self) -> CanonicalUnit:
        value = self.lower or self.upper or self.point
        assert value is not None
        return value.canonical_unit


@dataclass(frozen=True, slots=True)
class MeasurementSemantics:
    actual_role: ActualRole | None = None
    estimate_kind: EstimateKind | None = None
    contributor_id: str | None = None
    previous_period: Period | None = None

    def __post_init__(self) -> None:
        contract(self)
        if (self.actual_role is None) == (self.estimate_kind is None):
            raise ValidationError("exactly one actual or estimate role required")
        if (self.actual_role in (ActualRole.PREVIOUS, ActualRole.REVISED_PREVIOUS)) != (
            self.previous_period is not None
        ):
            raise ValidationError("previous numeric role requires an exact comparison period")
        if (
            self.contributor_id is not None
            and self.estimate_kind is not EstimateKind.INDIVIDUAL_ANALYST
        ):
            raise ValidationError("contributor identity requires individual-analyst semantics")


@dataclass(frozen=True, slots=True)
class RelationshipValue:
    relationship_type: RelationshipType
    source: ClaimSubject
    target: ClaimSubject
    exposure: ExposureMeasurement | None = None

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.source.kind is not SubjectKind.ENTITY
            or self.target.kind is not SubjectKind.ENTITY
            or self.source == self.target
        ):
            raise ValidationError("relationship claim needs distinct typed entities")


@dataclass(frozen=True, slots=True)
class ActionValue:
    payload: CorporatePayload

    def __post_init__(self) -> None:
        contract(self)


ClaimValue = (
    TimeValue
    | StatusValue
    | IdentityValue
    | OccurrenceValue
    | Quantity
    | NumericRange
    | RelationshipValue
    | ActionValue
    | FilingMetadata
)
NUMERIC = frozenset(
    {
        ClaimType.ACTUAL_VALUE,
        ClaimType.ESTIMATE_VALUE,
        ClaimType.GUIDANCE_VALUE,
        ClaimType.MACRO_VALUE,
    }
)
PREDICATES = {
    ClaimType.EVENT_OCCURRENCE: (Predicate.OCCURRED,),
    ClaimType.EVENT_TIME: (Predicate.SCHEDULED_FOR, Predicate.PUBLISHED_AT, Predicate.EFFECTIVE_AT),
    ClaimType.EVENT_STATUS: (Predicate.STATUS,),
    ClaimType.ENTITY_IDENTITY: (Predicate.IDENTIFIES,),
    ClaimType.RELATIONSHIP: (Predicate.RELATES_TO,),
    ClaimType.CORPORATE_ACTION: (Predicate.ACTION,),
    ClaimType.FILING_METADATA: (Predicate.FILING,),
    ClaimType.EARNINGS_SCHEDULE: (Predicate.SCHEDULED_FOR,),
    ClaimType.ACTUAL_VALUE: (Predicate.ACTUAL,),
    ClaimType.ESTIMATE_VALUE: (Predicate.ESTIMATE,),
    ClaimType.GUIDANCE_VALUE: (Predicate.GUIDANCE,),
    ClaimType.MACRO_VALUE: (Predicate.ACTUAL,),
}


@dataclass(frozen=True, slots=True)
class Claim:
    """A proposition slot; competing assertion values do not change its semantic ID."""

    claim_type: ClaimType
    subject: ClaimSubject
    predicate: Predicate
    measurement: MeasurementContext | None = None
    unit: CanonicalUnit | None = None
    measurement_semantics: MeasurementSemantics | None = None
    schema_version: str = CLAIM_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.schema_version != CLAIM_EVIDENCE_SCHEMA_VERSION
            or self.predicate not in PREDICATES[self.claim_type]
        ):
            raise ValidationError("supported claim family/predicate/schema required")
        if (self.claim_type in NUMERIC) != (self.measurement is not None and self.unit is not None):
            raise ValidationError("numeric claims require typed measurement and unit")
        if self.claim_type not in NUMERIC and (
            self.measurement is not None
            or self.unit is not None
            or self.measurement_semantics is not None
        ):
            raise ValidationError("measurement context only belongs to numeric claims")
        if (
            self.measurement is not None
            and self.subject.kind is SubjectKind.MEASUREMENT
            and self.subject.identity.startswith("evt_")
            and self.measurement.event_id != self.subject.identity
        ):
            raise ValidationError("measurement subject and event context disagree")
        if self.measurement is not None:
            code = self.measurement.measurement_id.code
            if (
                self.claim_type is ClaimType.MACRO_VALUE
                and not code.startswith("MACRO.")
                or self.claim_type is ClaimType.GUIDANCE_VALUE
                and not code.startswith("GUIDANCE.")
            ):
                raise ValidationError("numeric measurement and claim family disagree")
        if self.measurement_semantics is not None:
            semantics = self.measurement_semantics
            if (
                semantics.estimate_kind is not None
                and self.claim_type is not ClaimType.ESTIMATE_VALUE
                or semantics.actual_role is not None
                and self.claim_type not in (ClaimType.ACTUAL_VALUE, ClaimType.MACRO_VALUE)
            ):
                raise ValidationError("numeric role and claim family disagree")

    @property
    def claim_id(self) -> str:
        return "cclaim_" + fingerprint("eie:semantic-claim:v1", self)

    def validate_value(self, value: ClaimValue) -> None:
        expected: dict[ClaimType, tuple[type[object], ...]] = {
            ClaimType.EVENT_OCCURRENCE: (OccurrenceValue,),
            ClaimType.EVENT_TIME: (TimeValue,),
            ClaimType.EVENT_STATUS: (StatusValue,),
            ClaimType.ENTITY_IDENTITY: (IdentityValue,),
            ClaimType.RELATIONSHIP: (RelationshipValue,),
            ClaimType.CORPORATE_ACTION: (ActionValue,),
            ClaimType.FILING_METADATA: (FilingMetadata,),
            ClaimType.EARNINGS_SCHEDULE: (TimeValue,),
        }
        if not isinstance(
            value,
            (Quantity, NumericRange) if self.claim_type in NUMERIC else expected[self.claim_type],
        ):
            raise ValidationError("asserted value disagrees with claim family")
        unit = (
            value.canonical_unit
            if isinstance(value, Quantity)
            else value.canonical_unit
            if isinstance(value, NumericRange)
            else None
        )
        if unit is not None and unit != self.unit:
            raise ValidationError("competing numeric assertion has incompatible unit")


@dataclass(frozen=True, slots=True)
class ProviderClaimObservation:
    claim_id: str
    source: SourceIdentity
    source_family: str
    dataset_id: str
    resource_id: str
    provider_claim_key: str
    raw_observation_id: str
    lineage_id: str
    parser_version: str
    number: int
    known_from_ns: int
    value: ClaimValue
    evidence_ids: tuple[str, ...]
    status: ClaimStatus = ClaimStatus.ASSERTED
    previous_id: str | None = None
    valid_from_ns: int | None = None
    valid_to_ns: int | None = None
    event_id: str | None = None
    provenance: ProvenanceKind = ProvenanceKind.LIVE_OBSERVED
    reconstruction: ReconstructionQuality = ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME
    schema_version: str = CLAIM_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        identity(self.claim_id, ("cclaim",))
        identity(self.raw_observation_id, ("obs",))
        identity(self.lineage_id, ("eacq",))
        identity(self.dataset_id, ("eds",))
        ordered(self.evidence_ids, 4096, empty=False)
        for value in self.evidence_ids:
            identity(value, ("eref",))
        if self.event_id is not None:
            identity(self.event_id, ("evt",))
        if (
            self.schema_version != CLAIM_EVIDENCE_SCHEMA_VERSION
            or self.number < 1
            or (self.number == 1) != (self.previous_id is None)
        ):
            raise ValidationError("append-only provider revision predecessor required")
        if self.previous_id is not None:
            identity(self.previous_id, ("pclaim",))
        if self.number == 1 and self.status in (
            ClaimStatus.UPDATED,
            ClaimStatus.CORRECTED,
            ClaimStatus.RETRACTED,
            ClaimStatus.WITHDRAWN,
        ):
            raise ValidationError("revision action requires a prior assertion")
        if self.valid_to_ns is not None and (
            self.valid_from_ns is None or self.valid_to_ns <= self.valid_from_ns
        ):
            raise ValidationError("positive half-open claim validity required")

    @property
    def thread_id(self) -> str:
        return "cthread_" + fingerprint(
            "eie:provider-claim-thread:v1",
            (self.dataset_id, self.source.name, self.resource_id, self.provider_claim_key),
        )

    @property
    def observation_id(self) -> str:
        return "pclaim_" + fingerprint("eie:provider-claim-observation:v1", self)
