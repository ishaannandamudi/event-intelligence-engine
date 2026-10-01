"""Immutable histories and provider coverage assertions."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import MeasurementId, text
from eie.expectations.observations import ActualObservation, EstimateObservation


class EstimateAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE_ESTIMATE = "STALE_ESTIMATE"
    NO_ESTIMATE_AVAILABLE = "NO_ESTIMATE_AVAILABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    COVERAGE_UNKNOWN = "COVERAGE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class EstimateCoverage:
    provider_id: str
    event_ids: tuple[str, ...]
    measurements: tuple[MeasurementId, ...]
    has_vintages: bool
    available_from_ns: int

    def __post_init__(self) -> None:
        text(self.provider_id, "coverage provider")
        require_ns(self.available_from_ns)
        if (
            not isinstance(self.has_vintages, bool)
            or not isinstance(self.event_ids, tuple)
            or not isinstance(self.measurements, tuple)
        ):
            raise ValidationError("typed coverage required")
        if self.event_ids != tuple(sorted(set(self.event_ids))) or any(
            not v.startswith("evt_") for v in self.event_ids
        ):
            raise ValidationError("coverage event IDs must be sorted unique")
        if any(
            not isinstance(v, MeasurementId) for v in self.measurements
        ) or self.measurements != tuple(sorted(set(self.measurements), key=lambda v: v.code)):
            raise ValidationError("coverage measurements must be sorted unique")

    def covers(self, event_id: str, measurement: MeasurementId) -> bool:
        return event_id in self.event_ids and measurement in self.measurements


Observation = EstimateObservation | ActualObservation | GuidanceObservation


@dataclass(frozen=True, slots=True)
class ObservationHistory:
    as_of_ns: int
    observations: tuple[Observation, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns)
        if not isinstance(self.observations, tuple) or any(
            not isinstance(v, (EstimateObservation, ActualObservation, GuidanceObservation))
            for v in self.observations
        ):
            raise ValidationError("typed observation history required")
        if any(v.source.available_from_ns > self.as_of_ns for v in self.observations):
            raise TemporalConsistencyError("future observation in history")
        keys = tuple(
            (v.source.available_from_ns, v.stream_id, v.revision.number) for v in self.observations
        )
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("history must be sorted unique")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:expectation-history:v1", self)
