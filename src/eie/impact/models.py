"""Impact schema 1.0: explicit model policies and immutable derived contracts."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import require_schema
from eie.impact import IMPACT_SCHEMA_VERSION
from eie.impact.inputs import FeatureSnapshot, ImpactInputs, ReturnSemantics
from eie.studies.checks import contract, digest
from eie.studies.models import EventWindow, SessionPolicy
from eie.version import __version__


class ModelType(StrEnum):
    MARKET_ADJUSTED = "MARKET_ADJUSTED"
    SECTOR_ADJUSTED = "SECTOR_ADJUSTED"
    MARKET_MODEL = "MARKET_MODEL"
    SUPPLIED_FACTOR_MODEL = "SUPPLIED_FACTOR_MODEL"
    MATCHED_CONTROLS = "MATCHED_CONTROLS"


class FitMethod(StrEnum):
    IDENTITY_BENCHMARK = "IDENTITY_BENCHMARK"
    OLS_QR = "OLS_QR"
    STANDARDIZED_EUCLIDEAN = "STANDARDIZED_EUCLIDEAN"


class ContaminationPolicy(StrEnum):
    INCLUDE = "INCLUDE"
    EXCLUDE_EVENT = "EXCLUDE_EVENT"


class WeightMethod(StrEnum):
    EQUAL = "EQUAL"
    INVERSE_DISTANCE = "INVERSE_DISTANCE"


class FitStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT = "INSUFFICIENT"
    DEGENERATE = "DEGENERATE"
    SINGULAR = "SINGULAR"
    MISSING_INPUT = "MISSING_INPUT"
    NUMERIC_UNAVAILABLE = "NUMERIC_UNAVAILABLE"


class ImpactQuality(StrEnum):
    INSUFFICIENT_ESTIMATION_HISTORY = "INSUFFICIENT_ESTIMATION_HISTORY"
    LOW_R2 = "LOW_R2"
    DEGENERATE_BENCHMARK = "DEGENERATE_BENCHMARK"
    SINGULAR_FACTORS = "SINGULAR_FACTORS"
    POOR_CONTROL_MATCH = "POOR_CONTROL_MATCH"
    CONTROL_CONTAMINATION = "CONTROL_CONTAMINATION"
    TARGET_CONTAMINATION = "TARGET_CONTAMINATION"
    MISSING_BENCHMARK = "MISSING_BENCHMARK"
    MISSING_FACTORS = "MISSING_FACTORS"
    MISSING_EVENT_WINDOW_DATA = "MISSING_EVENT_WINDOW_DATA"
    DATA_GAP = "DATA_GAP"
    COARSE_EVENT_TIME = "COARSE_EVENT_TIME"
    RECONSTRUCTION_LIMITATION = "RECONSTRUCTION_LIMITATION"
    CORPORATE_ACTION_RISK = "CORPORATE_ACTION_RISK"
    NUMERIC_UNAVAILABLE = "NUMERIC_UNAVAILABLE"
    UNCERTAINTY_UNAVAILABLE = "UNCERTAINTY_UNAVAILABLE"
    FIT_CONTAMINATION_EXCLUDED = "FIT_CONTAMINATION_EXCLUDED"
    EVENT_EXCLUDED = "EVENT_EXCLUDED"


@dataclass(frozen=True, slots=True)
class EstimationWindow:
    start_offset: int
    end_offset: int
    gap_ns: int
    session_policy: SessionPolicy

    def __post_init__(self) -> None:
        contract(self)
        if not -(2**63 - 1) <= self.start_offset < self.end_offset < 0:
            raise ValidationError("estimation offsets must be ordered strictly prior to event")


@dataclass(frozen=True, slots=True)
class CARWindow:
    label: str
    start_offset: int
    end_offset: int
    aggregation: str = "SUM_PERIOD_ABNORMAL_RETURNS"

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.start_offset >= self.end_offset
            or self.aggregation != "SUM_PERIOD_ABNORMAL_RETURNS"
        ):
            raise ValidationError("CAR requires an independent nonempty summation window")


@dataclass(frozen=True, slots=True)
class MatchingPolicy:
    k: int = 5
    weighting: WeightMethod = WeightMethod.EQUAL
    exclude_contaminated: bool = True
    maximum_distance: float = 3.0
    minimum_scale: float = 1e-12
    minimum_prior_periods: int = 30

    def __post_init__(self) -> None:
        contract(self)
        if (
            not 1 <= self.k <= 100
            or self.maximum_distance <= 0
            or self.minimum_scale <= 0
            or self.minimum_prior_periods < 1
        ):
            raise ValidationError("invalid bounded matching policy")


@dataclass(frozen=True, slots=True)
class DatasetPin:
    role: str
    dataset_id: str
    fingerprint: str
    version: str
    member_id: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)


@dataclass(frozen=True, slots=True)
class CounterfactualModelSpec:
    model_type: ModelType
    estimation_window: EstimationWindow
    event_window: EventWindow
    car_windows: tuple[CARWindow, ...]
    semantics: ReturnSemantics
    event_build_id: str
    event_dataset_fingerprint: str
    study_id: str
    study_fingerprint: str
    input_fingerprint: str
    datasets: tuple[DatasetPin, ...]
    universe: tuple[str, ...]
    fit_method: FitMethod
    fit_cutoff_ns: int
    minimum_observations: int = 30
    intercept: bool = True
    contamination_policy: ContaminationPolicy = ContaminationPolicy.INCLUDE
    exclude_fit_contamination: bool = False
    matching: MatchingPolicy | None = None
    confidence_level: float = 0.95
    minimum_uncertainty_observations: int = 30
    low_r2_threshold: float = 0.1
    maximum_condition: float = 1e8
    model_version: str = "ols-qr-controls-1"
    engine_version: str = __version__
    schema_version: str = IMPACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        require_schema(self.schema_version, IMPACT_SCHEMA_VERSION)
        for d in (
            self.event_dataset_fingerprint,
            self.study_fingerprint,
            self.input_fingerprint,
        ):
            digest(d)
        w, e = self.estimation_window, self.event_window
        if (
            w.session_policy is not e.session_policy
            or w.end_offset > -e.pre_ns - w.gap_ns
            or self.semantics.timeframe is not e.timeframe
        ):
            raise ValidationError(
                "estimation frequency/clock or exclusion gap disagrees with event"
            )
        if (
            not self.car_windows
            or len(self.car_windows) > 100
            or tuple(c.label for c in self.car_windows)
            != tuple(sorted({c.label for c in self.car_windows}))
            or any(
                not -e.pre_ns <= c.start_offset < c.end_offset <= e.post_ns
                for c in self.car_windows
            )
        ):
            raise ValidationError("bounded sorted unique CAR windows must lie inside study")
        if self.datasets != tuple(
            sorted(
                set(self.datasets),
                key=lambda d: (d.role, d.member_id, d.dataset_id, d.fingerprint, d.version),
            )
        ):
            raise ValidationError("sorted unique dataset pins required")
        if self.universe != tuple(sorted(set(self.universe))):
            raise ValidationError("sorted unique model universe required")
        regression = self.model_type in (ModelType.MARKET_MODEL, ModelType.SUPPLIED_FACTOR_MODEL)
        wanted = (
            FitMethod.OLS_QR
            if regression
            else FitMethod.STANDARDIZED_EUCLIDEAN
            if self.model_type is ModelType.MATCHED_CONTROLS
            else FitMethod.IDENTITY_BENCHMARK
        )
        if self.fit_method is not wanted or (self.matching is not None) != (
            self.model_type is ModelType.MATCHED_CONTROLS
        ):
            raise ValidationError("model-specific fitting/matching policy required")
        if (
            not 2 <= self.minimum_observations <= 100_000
            or not 30 <= self.minimum_uncertainty_observations <= 100_000
            or not 0 < self.confidence_level < 1
            or not 0 <= self.low_r2_threshold <= 1
            or self.maximum_condition <= 1
        ):
            raise ValidationError("invalid explicit fit/uncertainty thresholds")

    @property
    def model_id(self) -> str:
        return "model_" + fingerprint("eie:counterfactual-model:v1", self)


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    start_ns: int
    end_ns: int
    value: float | None
    available_ns: int
    input_fingerprint: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.input_fingerprint)
        if not self.start_ns < self.end_ns <= self.available_ns:
            raise ValidationError("invalid completed period return")


@dataclass(frozen=True, slots=True)
class RegressionFit:
    names: tuple[str, ...]
    coefficients: tuple[float, ...]
    standard_errors: tuple[float, ...]
    covariance: tuple[tuple[float, ...], ...]
    residual_variance: float
    r_squared: float | None
    condition_number: float
    degrees_of_freedom: int
    residuals: tuple[float, ...]

    def __post_init__(self) -> None:
        contract(self)
        p = len(self.names)
        if (
            not p
            or p > 9
            or len(set(self.names)) != p
            or len(self.coefficients) != p
            or len(self.standard_errors) != p
            or len(self.covariance) != p
            or any(len(r) != p for r in self.covariance)
            or self.residual_variance < 0
            or any(s < 0 for s in self.standard_errors)
            or self.condition_number < 1
            or self.degrees_of_freedom < 1
        ):
            raise ValidationError("invalid regression diagnostics")


@dataclass(frozen=True, slots=True)
class BenchmarkFit:
    instrument_id: str
    coefficient: float = 1.0

    def __post_init__(self) -> None:
        contract(self)
        if self.coefficient != 1:
            raise ValidationError("adjusted benchmark must have unit coefficient")


@dataclass(frozen=True, slots=True)
class SelectedControl:
    instrument_id: str
    distance: float
    weight: float
    features: FeatureSnapshot
    contaminated: bool

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.distance < 0
            or not 0 <= self.weight <= 1
            or self.features.instrument_id != self.instrument_id
        ):
            raise ValidationError("invalid selected control")


@dataclass(frozen=True, slots=True)
class ControlFit:
    selected: tuple[SelectedControl, ...]
    scales: tuple[tuple[str, float], ...]
    scaling_members: tuple[str, ...]
    matching_ns: int
    exclusions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        contract(self)
        if (
            any(v <= 0 for _, v in self.scales)
            or len({c.instrument_id for c in self.selected}) != len(self.selected)
            or self.exclusions != tuple(sorted(set(self.exclusions)))
            or self.scaling_members != tuple(sorted(set(self.scaling_members)))
        ):
            raise ValidationError("invalid transparent control fit")


@dataclass(frozen=True, slots=True)
class ModelFitResult:
    status: FitStatus
    observation_count: int
    target_period_count: int
    missing_pair_count: int
    excluded_count: int
    period_fingerprints: tuple[str, ...]
    payload: RegressionFit | BenchmarkFit | ControlFit | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        if any(
            n < 0
            for n in (
                self.observation_count,
                self.target_period_count,
                self.missing_pair_count,
                self.excluded_count,
            )
        ):
            raise ValidationError("invalid fitting counts")
        if (self.status is FitStatus.AVAILABLE and self.payload is None) or (
            self.status is not FitStatus.AVAILABLE
            and self.payload is not None
            and not isinstance(self.payload, ControlFit)
        ):
            raise ValidationError("fit availability/payload disagree")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValidationError("sorted unique fit reasons required")
        for d in self.period_fingerprints:
            digest(d)


@dataclass(frozen=True, slots=True)
class UncertaintyInterval:
    confidence_level: float
    standard_error: float
    lower: float
    upper: float
    method: str = "NORMAL_APPROXIMATION_HOMOSKEDASTIC_OLS"

    def __post_init__(self) -> None:
        contract(self)
        if not 0 < self.confidence_level < 1 or self.standard_error < 0 or self.lower > self.upper:
            raise ValidationError("invalid uncertainty interval")


@dataclass(frozen=True, slots=True)
class AbnormalReturnPoint:
    offset_ns: int
    start_ns: int
    end_ns: int
    observed_return: float | None
    expected_return: float | None
    abnormal_return: float | None
    model_id: str
    input_fingerprints: tuple[str, ...]
    quality: frozenset[ImpactQuality]
    expected_uncertainty: UncertaintyInterval | None = None
    abnormal_uncertainty: UncertaintyInterval | None = None
    design: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if self.start_ns >= self.end_ns or (self.abnormal_return is not None) != (
            self.observed_return is not None and self.expected_return is not None
        ):
            raise ValidationError("abnormal return missingness/period mismatch")
        for d in self.input_fingerprints:
            digest(d)


@dataclass(frozen=True, slots=True)
class CumulativeAbnormalReturn:
    window: CARWindow
    start_ns: int
    end_ns: int
    value: float | None
    period_count: int
    uncertainty: UncertaintyInterval | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        if self.start_ns >= self.end_ns or self.period_count < 0:
            raise ValidationError("invalid CAR clocks/count")


@dataclass(frozen=True, slots=True)
class MetricCounterfactual:
    metric: str
    observed: float
    expected: float
    deviation: float
    method: str
    input_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class EventImpactResult:
    inputs: ImpactInputs
    model_spec: CounterfactualModelSpec
    fit: ModelFitResult
    abnormal_curve: tuple[AbnormalReturnPoint, ...]
    cars: tuple[CumulativeAbnormalReturn, ...]
    metrics: tuple[MetricCounterfactual, ...]
    quality: frozenset[ImpactQuality]
    available_ns: int
    uncertainty_assumptions: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.inputs.input_fingerprint != self.model_spec.input_fingerprint
            or self.inputs.study.study_id != self.model_spec.study_id
            or tuple(c.window for c in self.cars) != self.model_spec.car_windows
            or any(p.model_id != self.model_spec.model_id for p in self.abnormal_curve)
            or tuple(p.end_ns for p in self.abnormal_curve)
            != tuple(sorted({p.end_ns for p in self.abnormal_curve}))
            or not self.inputs.study.available_ns
            <= self.available_ns
            <= self.inputs.study.definition.data_as_of_ns
        ):
            raise ValidationError("impact specification/curve/provenance disagree")

    @property
    def impact_id(self) -> str:
        return "impact_" + fingerprint("eie:impact-identity:v1", self.model_spec)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:impact-result:v1", self)

    @property
    def event_id(self) -> str:
        return self.inputs.study.definition.event.event_id


@dataclass(frozen=True, slots=True)
class ImpactRunManifest:
    event_build_id: str
    event_dataset_fingerprint: str
    impact_ids: tuple[str, ...]
    result_fingerprints: tuple[str, ...]
    model_ids: tuple[str, ...]
    datasets: tuple[DatasetPin, ...]
    fit_failure_count: int
    contamination_count: int
    schema_version: str = IMPACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        require_schema(self.schema_version, IMPACT_SCHEMA_VERSION)
        digest(self.event_dataset_fingerprint)
        if (
            not self.impact_ids
            or self.impact_ids != tuple(sorted(set(self.impact_ids)))
            or len(self.impact_ids) != len(self.result_fingerprints)
            or len(self.impact_ids) != len(self.model_ids)
            or not 0 <= self.fit_failure_count <= len(self.impact_ids)
            or not 0 <= self.contamination_count <= len(self.impact_ids)
        ):
            raise ValidationError("invalid impact run membership/counts")
        for d in self.result_fingerprints:
            digest(d)

    @property
    def run_id(self) -> str:
        return "impact_run_" + fingerprint("eie:impact-run:v1", self)
