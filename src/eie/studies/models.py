"""Immutable descriptive study definitions, evidence and results (schema 1.0)."""

from dataclasses import dataclass
from enum import StrEnum

from eie.backfill.models import ReconstructionQuality
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import require_schema
from eie.core.time import require_ns
from eie.events.models import CanonicalEvent
from eie.expectations.surprise import SurpriseObservation
from eie.studies.checks import contract, digest, positive
from eie.studies.market import MarketSlice, Timeframe
from eie.version import __version__

STUDY_SCHEMA_VERSION = "1.0"


class AnchorSemantics(StrEnum):
    PUBLICATION = "PUBLICATION"
    ACCEPTANCE = "ACCEPTANCE"
    SCHEDULED = "SCHEDULED"
    SOURCE_AVAILABILITY = "SOURCE_AVAILABILITY"
    EIE_AVAILABILITY = "EIE_AVAILABILITY"


class AnchorQuality(StrEnum):
    EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
    DATE_ONLY = "DATE_ONLY"
    SESSION_LABEL = "SESSION_LABEL"
    UNKNOWN = "UNKNOWN"


class CoarsePolicy(StrEnum):
    REJECT = "REJECT"
    SESSION_OPEN = "SESSION_OPEN"
    SESSION_CLOSE = "SESSION_CLOSE"


class SessionPolicy(StrEnum):
    ELAPSED = "ELAPSED"
    TRADING = "TRADING"


class AlignmentPolicy(StrEnum):
    LAST_COMPLETED = "LAST_COMPLETED"
    FIRST_BEGINNING = "FIRST_BEGINNING"


class ReferencePolicy(StrEnum):
    PRE_CLOSE = "PRE_CLOSE"
    FIRST_POST_OPEN = "FIRST_POST_OPEN"
    ALIGNED_CLOSE = "ALIGNED_CLOSE"


class ReturnMethod(StrEnum):
    SIMPLE = "SIMPLE"
    LOG = "LOG"


class StudyQuality(StrEnum):
    MISSING_PRE_DATA = "MISSING_PRE_DATA"
    MISSING_POST_DATA = "MISSING_POST_DATA"
    COARSE_EVENT_TIME = "COARSE_EVENT_TIME"
    MARKET_CLOSED = "MARKET_CLOSED"
    PARTIAL_SESSION = "PARTIAL_SESSION"
    MISSING_BENCHMARK = "MISSING_BENCHMARK"
    MISSING_QUOTES = "MISSING_QUOTES"
    MISSING_OPTIONS = "MISSING_OPTIONS"
    CONTAMINATED = "CONTAMINATED"
    DATA_GAP = "DATA_GAP"
    LOW_OBSERVATION_COUNT = "LOW_OBSERVATION_COUNT"
    EVENT_HISTORY_INCOMPLETE = "EVENT_HISTORY_INCOMPLETE"
    CORPORATE_ACTION_RISK = "CORPORATE_ACTION_RISK"
    NUMERIC_UNAVAILABLE = "NUMERIC_UNAVAILABLE"
    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"


class BenchmarkType(StrEnum):
    MARKET_INDEX = "MARKET_INDEX"
    SECTOR_INDEX = "SECTOR_INDEX"
    INDUSTRY_INDEX = "INDUSTRY_INDEX"
    ETF_PROXY = "ETF_PROXY"


@dataclass(frozen=True, slots=True)
class EventAnchor:
    semantics: AnchorSemantics
    quality: AnchorQuality
    timestamp_ns: int | None = None
    session_label: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        if (self.timestamp_ns is not None) != (self.quality is AnchorQuality.EXACT_TIMESTAMP):
            raise ValidationError("coarse anchor cannot carry an invented timestamp")
        if self.timestamp_ns is not None:
            require_ns(self.timestamp_ns)
        if (
            self.quality in (AnchorQuality.DATE_ONLY, AnchorQuality.SESSION_LABEL)
            and self.session_label is None
        ):
            raise ValidationError("coarse study requires explicit supplied session label")
        if self.quality is AnchorQuality.EXACT_TIMESTAMP and self.session_label is not None:
            raise ValidationError("exact anchor must not carry a coarse label")


@dataclass(frozen=True, slots=True)
class EventWindow:
    anchor_semantics: AnchorSemantics
    pre_ns: int
    post_ns: int
    timeframe: Timeframe
    session_policy: SessionPolicy
    offsets_ns: tuple[int, ...]
    coarse_policy: CoarsePolicy = CoarsePolicy.REJECT

    def __post_init__(self) -> None:
        contract(self)
        positive(self.pre_ns)
        positive(self.post_ns)
        require_ns(self.pre_ns)
        require_ns(self.post_ns)
        if (
            not self.offsets_ns
            or self.offsets_ns != tuple(sorted(set(self.offsets_ns)))
            or any(not -self.pre_ns <= o <= self.post_ns for o in self.offsets_ns)
        ):
            raise ValidationError("response offsets must be sorted unique and within window")
        if len(self.offsets_ns) > 1000:
            raise ValidationError("bounded response offsets required")

    @property
    def window_id(self) -> str:
        return "win_" + fingerprint("eie:study-window:v1", self)


@dataclass(frozen=True, slots=True)
class StudyPolicy:
    return_method: ReturnMethod = ReturnMethod.SIMPLE
    reference: ReferencePolicy = ReferencePolicy.PRE_CLOSE
    alignment: AlignmentPolicy = AlignmentPolicy.LAST_COMPLETED
    max_lag_ns: int = 60 * 10**9
    minimum_volatility_returns: int = 2
    volume_baseline_ns: int = 20 * 86400 * 10**9
    minimum_baseline_bars: int = 3
    maximum_input_observations: int = 100_000
    contamination_pre_ns: int = 60 * 10**9
    contamination_post_ns: int = 240 * 10**9
    required_quality: ReconstructionQuality | None = None
    options_key: tuple[str, str, str] | None = None

    def __post_init__(self) -> None:
        contract(self)
        for t in (
            self.max_lag_ns,
            self.volume_baseline_ns,
            self.contamination_pre_ns,
            self.contamination_post_ns,
        ):
            require_ns(t)
        positive(self.minimum_volatility_returns)
        positive(self.minimum_baseline_bars)
        positive(self.maximum_input_observations)
        if self.maximum_input_observations > 100_000:
            raise ValidationError("bounded study input observations required")

    @property
    def policy_id(self) -> str:
        return "pol_" + fingerprint("eie:study-policy:v1", self)


@dataclass(frozen=True, slots=True)
class ContextLabel:
    dimension: str
    label: str
    source: str
    version: str
    timestamp_ns: int
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.timestamp_ns)
        require_ns(self.available_ns)
        if self.timestamp_ns > self.available_ns:
            raise ValidationError("context availability precedes observation")


@dataclass(frozen=True, slots=True)
class StudyDefinition:
    event: CanonicalEvent
    event_build_id: str
    event_dataset_fingerprint: str
    event_gap_fingerprints: tuple[str, ...]
    reconstruction_quality: ReconstructionQuality
    selection_as_of_ns: int
    data_as_of_ns: int
    anchor: EventAnchor
    window: EventWindow
    policy: StudyPolicy
    historical_fact_id: str
    context: tuple[ContextLabel, ...] = ()
    surprise: SurpriseObservation | None = None
    engine_version: str = __version__
    schema_version: str = STUDY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        require_schema(self.schema_version, STUDY_SCHEMA_VERSION)
        if not self.historical_fact_id.startswith("fact_"):
            raise ValidationError("historical event fact identity required")
        digest(self.historical_fact_id[5:])
        digest(self.event_dataset_fingerprint)
        for d in self.event_gap_fingerprints:
            digest(d)
        if self.event_gap_fingerprints != tuple(sorted(set(self.event_gap_fingerprints))):
            raise ValidationError("event gaps must be sorted unique")
        require_ns(self.selection_as_of_ns)
        require_ns(self.data_as_of_ns)
        if (
            self.event.revision.available_from_ns > self.selection_as_of_ns
            or self.selection_as_of_ns > self.data_as_of_ns
        ):
            raise ValidationError("event revision must be visible at study selection cutoff")
        if self.anchor.semantics is not self.window.anchor_semantics:
            raise ValidationError("anchor/window semantics mismatch")
        if (
            self.policy.required_quality is not None
            and self.reconstruction_quality is not self.policy.required_quality
        ):
            raise ValidationError("historical reconstruction quality fails study policy")
        if self.context != tuple(sorted(self.context, key=lambda c: c.dimension)) or len(
            {c.dimension for c in self.context}
        ) != len(self.context):
            raise ValidationError("unique sorted context dimensions required")
        if self.surprise is not None and self.surprise.context.event_id != self.event.event_id:
            raise ValidationError("surprise belongs to another event")


@dataclass(frozen=True, slots=True)
class ResponsePoint:
    offset_ns: int
    target_ns: int
    observed_ns: int | None
    value: float | None

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.target_ns)
        if self.observed_ns is not None:
            require_ns(self.observed_ns)
        if (self.observed_ns is None) != (self.value is None):
            raise ValidationError("response missingness must retain actual observation time")


@dataclass(frozen=True, slots=True)
class VolatilityResponse:
    pre: float | None
    post: float | None
    difference: float | None
    ratio: float | None
    pre_count: int
    post_count: int

    def __post_init__(self) -> None:
        contract(self)
        positive(self.pre_count, zero=True)
        positive(self.post_count, zero=True)
        if any(v is not None and v < 0 for v in (self.pre, self.post, self.ratio)):
            raise ValidationError("negative realized volatility")


@dataclass(frozen=True, slots=True)
class VolumePoint:
    bar_start_ns: int
    volume: float
    cumulative: float
    baseline: float | None
    relative: float | None
    baseline_count: int

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.bar_start_ns)
        positive(self.baseline_count, zero=True)
        if any(
            v is not None and v < 0
            for v in (self.volume, self.cumulative, self.baseline, self.relative)
        ):
            raise ValidationError("negative volume response")


@dataclass(frozen=True, slots=True)
class LiquidityResponse:
    pre_timestamp_ns: int | None
    post_timestamp_ns: int | None
    pre_spread: float | None
    post_spread: float | None
    relative_spread: float | None
    quoted_depth: float | None
    midpoint_return: float | None

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class OptionsResponse:
    key: tuple[str, str, str] | None
    pre_timestamp_ns: int | None
    post_timestamp_ns: int | None
    iv_change: float | None
    skew_change: float | None
    volume_change: float | None
    open_interest_change: float | None

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class ContaminationFlag:
    event_id: str
    revision_id: str
    timestamp_ns: int
    time_basis: str
    reasons: tuple[str, ...]
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.timestamp_ns)
        require_ns(self.available_ns)
        if not self.reasons or self.reasons != tuple(sorted(set(self.reasons))):
            raise ValidationError("contamination reasons must be sorted unique")


@dataclass(frozen=True, slots=True)
class BenchmarkInput:
    kind: BenchmarkType
    instrument_id: str
    data: MarketSlice | None

    def __post_init__(self) -> None:
        contract(self)
        if self.data is not None and self.data.identity.instrument_id != self.instrument_id:
            raise ValidationError("benchmark instrument mismatch")


@dataclass(frozen=True, slots=True)
class BenchmarkResponse:
    kind: BenchmarkType
    instrument_id: str
    curve: tuple[ResponsePoint, ...]
    volatility: VolatilityResponse | None
    descriptive_difference: float | None
    quality: frozenset[StudyQuality]

    def __post_init__(self) -> None:
        contract(self)


@dataclass(frozen=True, slots=True)
class EventStudyResult:
    definition: StudyDefinition
    market: MarketSlice
    benchmarks: tuple[BenchmarkInput, ...]
    anchor_ns: int
    pre_start_ns: int
    post_end_ns: int
    reference_price: float | None
    reference_ns: int | None
    curve: tuple[ResponsePoint, ...]
    pre_return: float | None
    post_return: float | None
    volatility: VolatilityResponse
    volume: tuple[VolumePoint, ...]
    liquidity: LiquidityResponse
    options: OptionsResponse
    benchmark_responses: tuple[BenchmarkResponse, ...]
    contamination: tuple[ContaminationFlag, ...]
    quality: frozenset[StudyQuality]
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        for t in (self.anchor_ns, self.pre_start_ns, self.post_end_ns, self.available_ns):
            require_ns(t)
        if (
            not self.pre_start_ns
            < self.anchor_ns
            < self.post_end_ns
            <= self.definition.data_as_of_ns
            or self.available_ns > self.definition.data_as_of_ns
            or self.available_ns < self.post_end_ns
        ):
            raise ValidationError("invalid study window/availability")
        if self.market.identity.timeframe is not self.definition.window.timeframe:
            raise ValidationError("market/window timeframe mismatch")
        if tuple(p.offset_ns for p in self.curve) != self.definition.window.offsets_ns:
            raise ValidationError("response curve/window mismatch")
        if tuple((b.kind, b.instrument_id) for b in self.benchmarks) != tuple(
            sorted({(b.kind, b.instrument_id) for b in self.benchmarks})
        ):
            raise ValidationError("sorted unique benchmarks required")
        if self.contamination != tuple(
            sorted(
                set(self.contamination), key=lambda c: (c.timestamp_ns, c.event_id, c.revision_id)
            )
        ):
            raise ValidationError("sorted unique contamination required")

    @property
    def study_id(self) -> str:
        return "study_" + fingerprint(
            "eie:study-identity:v1",
            (
                self.definition,
                self.market.identity,
                tuple(
                    (b.kind, b.instrument_id, None if b.data is None else b.data.identity)
                    for b in self.benchmarks
                ),
            ),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:study-result:v1", self)


@dataclass(frozen=True, slots=True)
class StudyRunManifest:
    event_build_id: str
    event_dataset_fingerprint: str
    study_ids: tuple[str, ...]
    result_fingerprints: tuple[str, ...]
    contamination_count: int
    missing_data_count: int
    schema_version: str = STUDY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        require_schema(self.schema_version, STUDY_SCHEMA_VERSION)
        digest(self.event_dataset_fingerprint)
        if self.study_ids != tuple(sorted(set(self.study_ids))) or len(self.study_ids) != len(
            self.result_fingerprints
        ):
            raise ValidationError("invalid run study identities")
        for d in self.result_fingerprints:
            digest(d)
        positive(self.contamination_count, zero=True)
        positive(self.missing_data_count, zero=True)

    @property
    def run_id(self) -> str:
        return "run_" + fingerprint("eie:study-run:v1", self)
