"""Immutable estimate and actual vintages backed by raw source evidence."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.schema import EXPECTATIONS_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.events.revisions import EventRevision
from eie.expectations.models import MeasurementContext, ObservationSource, Period, flags, text
from eie.expectations.units import Dimension, Quantity


class EstimateKind(StrEnum):
    INDIVIDUAL_ANALYST = "INDIVIDUAL_ANALYST"
    PROVIDER_CONSENSUS_MEAN = "PROVIDER_CONSENSUS_MEAN"
    PROVIDER_CONSENSUS_MEDIAN = "PROVIDER_CONSENSUS_MEDIAN"
    PROVIDER_CONSENSUS_HIGH = "PROVIDER_CONSENSUS_HIGH"
    PROVIDER_CONSENSUS_LOW = "PROVIDER_CONSENSUS_LOW"
    PROVIDER_CONSENSUS_COUNT = "PROVIDER_CONSENSUS_COUNT"
    COMPANY_GUIDANCE = "COMPANY_GUIDANCE"
    OTHER_STRUCTURED_EXPECTATION = "OTHER_STRUCTURED_EXPECTATION"


class ActualRole(StrEnum):
    ACTUAL = "ACTUAL"
    PREVIOUS = "PREVIOUS"
    REVISED_PREVIOUS = "REVISED_PREVIOUS"


def validate_observation(
    context: MeasurementContext,
    source: ObservationSource,
    revision: EventRevision,
    quality_flags: frozenset[QualityFlag],
    schema_version: str,
) -> None:
    require_schema(schema_version, EXPECTATIONS_SCHEMA_VERSION)
    if (
        not isinstance(context, MeasurementContext)
        or not isinstance(source, ObservationSource)
        or not isinstance(revision, EventRevision)
    ):
        raise ValidationError("typed observation context/source/revision required")
    flags(quality_flags)
    if revision.available_from_ns != source.available_from_ns:
        raise TemporalConsistencyError("revision and source availability disagree")


@dataclass(frozen=True, slots=True)
class EstimateObservation:
    context: MeasurementContext
    source: ObservationSource
    value: Quantity
    estimate_type: EstimateKind
    revision: EventRevision
    contributor_id: str | None = None
    contributor_count: int | None = None
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = EXPECTATIONS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_observation(
            self.context, self.source, self.revision, self.quality_flags, self.schema_version
        )
        if not isinstance(self.value, Quantity) or not isinstance(self.estimate_type, EstimateKind):
            raise ValidationError("typed estimate value and kind required")
        if self.contributor_id is not None:
            text(self.contributor_id, "contributor ID")
            if self.estimate_type is not EstimateKind.INDIVIDUAL_ANALYST:
                raise ValidationError("contributor ID only identifies an individual estimate")
        if self.contributor_count is not None and (
            isinstance(self.contributor_count, bool)
            or not isinstance(self.contributor_count, int)
            or self.contributor_count < 0
        ):
            raise ValidationError("contributor count must be nonnegative integer")
        if self.estimate_type is EstimateKind.PROVIDER_CONSENSUS_COUNT and (
            self.value.canonical_unit.dimension is not Dimension.COUNT
            or self.value.canonical_value < 0
            or self.value.canonical_value % 1
        ):
            raise ValidationError("consensus count requires nonnegative integer count")

    @property
    def stream_id(self) -> str:
        return "est_" + fingerprint(
            "eie:estimate-stream:v1",
            (
                self.context,
                self.source.provider_id,
                self.source.provider_record_id,
                self.estimate_type,
                self.contributor_id,
            ),
        )

    @property
    def estimate_observation_id(self) -> str:
        return "eobs_" + fingerprint("eie:estimate-observation:v1", self)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:estimate-result:v1", self)


@dataclass(frozen=True, slots=True)
class ActualObservation:
    context: MeasurementContext
    source: ObservationSource
    value: Quantity
    role: ActualRole
    published_at_ns: int
    revision: EventRevision
    previous_reference_period: Period | None = None
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = EXPECTATIONS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_observation(
            self.context, self.source, self.revision, self.quality_flags, self.schema_version
        )
        if not isinstance(self.value, Quantity) or not isinstance(self.role, ActualRole):
            raise ValidationError("typed actual value and role required")
        require_ns(self.published_at_ns)
        if self.published_at_ns > self.source.provider_observed_at_ns:
            raise TemporalConsistencyError("actual publication follows observation")
        if self.role is ActualRole.ACTUAL:
            if self.previous_reference_period is not None:
                raise ValidationError("actual cannot carry previous reference period")
        elif self.previous_reference_period is None:
            raise ValidationError("previous value requires explicit comparison period")
        else:
            MeasurementContext(
                self.context.event_id, self.context.measurement_id, self.previous_reference_period
            )

    @property
    def stream_id(self) -> str:
        return "act_" + fingerprint(
            "eie:actual-stream:v1",
            (
                self.context,
                self.source.provider_id,
                self.source.provider_record_id,
                self.role,
                self.previous_reference_period,
            ),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:actual-result:v1", self)
