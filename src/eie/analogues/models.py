"""Immutable named features, pinned inputs and bounded transparent results."""

from dataclasses import dataclass

from eie.analogues import HISTORICAL_ANALOGUE_SCHEMA_VERSION
from eie.analogues.schema import (
    ContaminationPolicy,
    Exclusion,
    FeatureKind,
    FeatureSource,
    MissingPolicy,
    ResponseMetric,
    ResultState,
    ScaleMethod,
    SectorCondition,
    TypeCondition,
    UncertaintyKind,
    UncertaintyState,
    ValidationState,
    ZeroScalePolicy,
)
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import ordered
from eie.studies.checks import contract, digest
from eie.version import __version__

FeatureScalar = str | float | bool | None


def label(value: str) -> None:
    if not value or value != value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValidationError("bounded analogue label required")


class Artifact:
    __slots__ = ()

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:analogue:" + type(self).__name__ + ":v1", self)


@dataclass(frozen=True, slots=True)
class ContextFeature(Artifact):
    dimension: str
    value: FeatureScalar
    source: str
    version: str
    observed_ns: int
    available_ns: int
    unit: str = "CATEGORY"
    valid_from_ns: int | None = None
    valid_to_ns: int | None = None

    def __post_init__(self) -> None:
        contract(self)
        if isinstance(self.value, str):
            label(self.value)
        if self.observed_ns > self.available_ns or (
            self.valid_from_ns is not None
            and self.valid_to_ns is not None
            and self.valid_from_ns >= self.valid_to_ns
        ):
            raise ValidationError("invalid context clocks")

    def visible(self, cutoff_ns: int, valid_ns: int) -> bool:
        return (
            self.available_ns <= cutoff_ns
            and self.observed_ns <= cutoff_ns
            and (self.valid_from_ns is None or self.valid_from_ns <= valid_ns)
            and (self.valid_to_ns is None or valid_ns < self.valid_to_ns)
        )


@dataclass(frozen=True, slots=True)
class AnalogueFeature(Artifact):
    name: str
    kind: FeatureKind
    source: FeatureSource
    selector: str
    weight: float = 1.0
    missing: MissingPolicy = MissingPolicy.REQUIRE
    missing_penalty: float = 1.0
    scale: ScaleMethod = ScaleMethod.MAD
    zero_scale: ZeroScalePolicy = ZeroScalePolicy.EXACT_MATCH
    ordinal_labels: tuple[str, ...] = ()
    measurement: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        for v in self.ordinal_labels:
            label(v)
        if self.source is FeatureSource.INFORMATION_SURPRISE and (
            self.kind is not FeatureKind.NUMERIC
            or self.selector != "normalized_surprise"
            or self.measurement is None
        ):
            raise ValidationError("information feature requires named normalized measurement")
        if (
            not 0 < self.weight <= 1_000_000
            or not 0 <= self.missing_penalty <= 1
            or (
                self.kind is FeatureKind.ORDINAL
                and (
                    not 2 <= len(self.ordinal_labels) <= 32
                    or len(set(self.ordinal_labels)) != len(self.ordinal_labels)
                )
            )
            or (self.kind is not FeatureKind.ORDINAL and self.ordinal_labels)
        ):
            raise ValidationError("invalid feature weight/penalty/ordinal comparison")


DEFAULT_FEATURES = (
    AnalogueFeature("event_type", FeatureKind.CATEGORICAL, FeatureSource.EVENT_TYPE, "type"),
    AnalogueFeature(
        "materiality",
        FeatureKind.CATEGORICAL,
        FeatureSource.MATERIALITY,
        "category",
        missing=MissingPolicy.IGNORE_WITH_PENALTY,
    ),
)


@dataclass(frozen=True, slots=True)
class AnaloguePolicy(Artifact):
    features: tuple[AnalogueFeature, ...] = DEFAULT_FEATURES
    type_condition: TypeCondition = TypeCondition.EXACT
    allowed_types: tuple[str, ...] = ()
    sector_condition: SectorCondition = SectorCondition.REQUIRE_SAME
    same_issuer: bool = False
    blocking_dimensions: tuple[str, ...] = ()
    contamination: ContaminationPolicy = ContaminationPolicy.EXCLUDE
    contaminated_cohort: bool = False
    reject_data_gaps: bool = True
    reject_coarse_time: bool = True
    reject_reconstruction: bool = False
    minimum_comparable_features: int = 1
    minimum_weight_fraction: float = 0.5
    minimum_scale_samples: int = 5
    minimum_history: int = 5
    maximum_distance: float = 2.0
    poor_distance_threshold: float = 1.0
    stability_threshold: float = 0.02
    version: str = "transparent-analogue-1"

    def __post_init__(self) -> None:
        contract(self)
        ordered(tuple(f.name for f in self.features), 64, empty=False)
        ordered(self.allowed_types, 256)
        ordered(self.blocking_dimensions, 32)
        if (self.type_condition is TypeCondition.ALLOWED_SET) != bool(self.allowed_types):
            raise ValidationError("explicit allowed type set required")
        if (
            not 1 <= self.minimum_comparable_features <= len(self.features)
            or not 0 < self.minimum_weight_fraction <= 1
            or not 2 <= self.minimum_scale_samples <= 10000
            or not 1 <= self.minimum_history <= 1000
            or self.maximum_distance < 0
            or self.poor_distance_threshold < 0
            or self.stability_threshold < 0
        ):
            raise ValidationError("invalid analogue thresholds")

    @property
    def policy_id(self) -> str:
        return "analogue_policy_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class AnalogueLimits:
    pool_candidates: int = 10000
    selected: int = 100
    features: int = 64
    contexts: int = 32
    responses: int = 1000
    uncertainty_reasons: int = 64
    query_output_bytes: int = 10_000_000

    def __post_init__(self) -> None:
        contract(self)
        if not all(
            1 <= v <= maximum
            for v, maximum in (
                (self.pool_candidates, 100000),
                (self.selected, 1000),
                (self.features, 64),
                (self.contexts, 32),
                (self.responses, 1000),
                (self.uncertainty_reasons, 64),
                (self.query_output_bytes, 50_000_000),
            )
        ):
            raise ValidationError("bounded analogue operations required")


@dataclass(frozen=True, slots=True)
class FeatureValue:
    name: str
    value: FeatureScalar
    signature: str
    input_ids: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.input_ids, 128)
        if isinstance(self.value, str):
            label(self.value)
        if (self.value is None) != (self.reason is not None):
            raise ValidationError("missing feature requires reason")


@dataclass(frozen=True, slots=True)
class FeatureRow(Artifact):
    event_id: str
    revision: int
    event_revision_id: str
    event_fingerprint: str
    dataset_id: str
    dataset_fingerprint: str
    build_id: str | None
    event_type: str
    family: str
    entity_ids: tuple[str, ...]
    alias_event_ids: tuple[str, ...]
    event_available_ns: int
    feature_cutoff_ns: int
    available_ns: int
    intelligence_id: str
    intelligence_fingerprint: str
    policy_id: str
    values: tuple[FeatureValue, ...]
    contexts: tuple[ContextFeature, ...]
    sector: str | None
    industry: str | None
    materiality: str
    measurement_ids: tuple[str, ...]
    graph_id: str | None
    reconstruction_reasons: tuple[str, ...]
    information_ids: tuple[str, ...]
    quality: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        for value in (
            self.event_fingerprint,
            self.dataset_fingerprint,
            self.intelligence_fingerprint,
        ):
            digest(value)
        for values, bound in (
            (self.entity_ids, 1000),
            (self.alias_event_ids, 1000),
            (self.measurement_ids, 128),
            (self.reconstruction_reasons, 64),
            (self.information_ids, 128),
            (self.quality, 64),
        ):
            ordered(values, bound)
        ordered(tuple(v.name for v in self.values), 64, empty=False)
        ordered(tuple(v.dimension for v in self.contexts), 32)
        if (
            self.revision < 1
            or self.event_id not in self.alias_event_ids
            or self.feature_cutoff_ns > self.event_available_ns
            or self.available_ns < max(self.event_available_ns, self.feature_cutoff_ns)
            or any(
                not c.visible(self.feature_cutoff_ns, self.event_available_ns)
                for c in self.contexts
            )
        ):
            raise ValidationError("ex-ante feature row clocks/context invalid")

    @property
    def row_id(self) -> str:
        return "analogue_features_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class ResponseSpec(Artifact):
    metric: ResponseMetric
    study_cohort: str
    offset_ns: int
    model_cohort: str | None = None
    car_label: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.study_cohort)
        if self.model_cohort is not None:
            digest(self.model_cohort)
        impact = self.metric in (
            ResponseMetric.ABNORMAL_RETURN,
            ResponseMetric.ABS_ABNORMAL_RETURN,
            ResponseMetric.CAR,
        )
        if (
            impact != (self.model_cohort is not None)
            or ((self.metric is ResponseMetric.CAR) != (self.car_label is not None))
            or self.offset_ns < 0
        ):
            raise ValidationError("response must pin compatible metric/window/model")


@dataclass(frozen=True, slots=True)
class ResponseObservation(Artifact):
    feature_row_id: str
    event_id: str
    revision: int
    spec: ResponseSpec
    study_id: str
    study_fingerprint: str
    impact_id: str | None
    model_id: str | None
    impact_fingerprint: str | None
    value: float | None
    completed_ns: int
    available_ns: int
    contaminated: bool
    quality: tuple[str, ...]
    reconstruction_reasons: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.study_fingerprint)
        if self.impact_fingerprint is not None:
            digest(self.impact_fingerprint)
        ordered(self.quality, 64)
        ordered(self.reconstruction_reasons, 64)
        if (
            self.completed_ns > self.available_ns
            or self.revision < 1
            or (self.value is None) != (self.reason is not None)
            or (self.impact_id is not None) != (self.spec.model_cohort is not None)
            or (self.impact_id is not None) != (self.model_id is not None)
            or (self.impact_id is not None) != (self.impact_fingerprint is not None)
        ):
            raise ValidationError("invalid response provenance/availability")

    @property
    def response_id(self) -> str:
        return "analogue_response_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class AnalogueQuery(Artifact):
    target_row_id: str
    target_event_id: str
    target_revision: int
    dataset_id: str
    known_as_of_ns: int
    available_ns: int
    policy_id: str
    response: ResponseSpec
    top_k: int = 20
    earliest_ns: int = 0
    event_resolution_id: str | None = None
    schema_version: str = HISTORICAL_ANALOGUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        if (
            not 1 <= self.top_k <= 1000
            or self.target_revision < 1
            or self.available_ns < self.known_as_of_ns
            or self.earliest_ns > self.known_as_of_ns
            or self.schema_version != HISTORICAL_ANALOGUE_SCHEMA_VERSION
        ):
            raise ValidationError("invalid bounded query/cutoff/schema")

    @property
    def query_id(self) -> str:
        return "analogue_query_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class NumericScale:
    feature: str
    method: ScaleMethod
    count: int
    value: float | None
    reason: str | None
    member_fingerprint: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.member_fingerprint)
        if (
            self.count < 0
            or (self.value is None) != (self.reason is not None)
            or (self.value is not None and self.value < 0)
        ):
            raise ValidationError("invalid prior scale")


@dataclass(frozen=True, slots=True)
class DistanceComponent:
    feature: str
    kind: FeatureKind
    target: FeatureScalar
    candidate: FeatureScalar
    weight: float
    distance: float | None
    scale: float | None
    penalty: float
    rule: str
    reason: str | None

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.weight <= 0
            or self.penalty < 0
            or (self.distance is not None and self.distance < 0)
        ):
            raise ValidationError("invalid transparent distance component")


@dataclass(frozen=True, slots=True)
class AnalogueCandidate(Artifact):
    row_id: str
    event_id: str
    revision: int
    event_available_ns: int
    response_id: str
    response_available_ns: int
    features: tuple[FeatureValue, ...]
    components: tuple[DistanceComponent, ...]
    total_distance: float
    numerator: float
    comparable_weight: float
    total_weight: float
    missing_penalty: float
    comparable_count: int
    response_value: float
    contaminated: bool
    quality: tuple[str, ...]
    rule: str = "(SUM_WEIGHTED_COMPONENTS+SUM_MISSING_PENALTIES)/TOTAL_CONFIGURED_WEIGHT"

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.quality, 64)
        ordered(tuple(v.name for v in self.features), 64, empty=False)
        ordered(tuple(v.feature for v in self.components), 64, empty=False)
        if (
            self.revision < 1
            or self.total_distance < 0
            or self.numerator < 0
            or self.total_weight <= 0
            or not 0 < self.comparable_weight <= self.total_weight
            or self.comparable_count < 1
            or self.missing_penalty < 0
        ):
            raise ValidationError("invalid comparable candidate")


@dataclass(frozen=True, slots=True)
class PoolMember:
    row_id: str
    row_fingerprint: str
    response_id: str | None
    response_fingerprint: str | None
    exclusion: Exclusion | None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.row_fingerprint)
        if self.response_fingerprint is not None:
            digest(self.response_fingerprint)
        if (self.response_id is None) != (self.response_fingerprint is None):
            raise ValidationError("pool response pin incomplete")


@dataclass(frozen=True, slots=True)
class PoolDiagnostics:
    total_prior_events: int
    blocked_candidates: int
    eligible: int
    response_complete: int
    selected: int
    excluded: tuple[tuple[Exclusion, int], ...]
    nearest_distance: float | None
    median_distance: float | None
    context_coverage: tuple[tuple[str, int, int], ...]
    stability_medians: tuple[tuple[int, float], ...]

    def __post_init__(self) -> None:
        contract(self)
        if (
            min(
                self.total_prior_events,
                self.blocked_candidates,
                self.eligible,
                self.response_complete,
                self.selected,
            )
            < 0
            or self.excluded != tuple(sorted(set(self.excluded)))
            or any(n < 0 for _, n in self.excluded)
        ):
            raise ValidationError("invalid analogue pool counts")


@dataclass(frozen=True, slots=True)
class ReactionDistribution(Artifact):
    response_spec: ResponseSpec
    candidate_ids: tuple[str, ...]
    raw_values: tuple[float, ...]
    mean: float | None
    median: float | None
    quantiles: tuple[tuple[float, float], ...]
    mad: float | None
    positive_frequency: float | None
    negative_frequency: float | None
    median_absolute: float | None
    minimum: float | None
    maximum: float | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64)
        if (
            len(self.candidate_ids) != len(self.raw_values)
            or len(self.raw_values) > 1000
            or len(set(self.candidate_ids)) != len(self.candidate_ids)
        ):
            raise ValidationError("bounded distinct distribution membership required")

    @property
    def count(self) -> int:
        return len(self.raw_values)


@dataclass(frozen=True, slots=True)
class AnalogueUncertainty:
    kind: UncertaintyKind
    state: UncertaintyState
    reasons: tuple[str, ...]
    diagnostics: tuple[tuple[str, float | None], ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64, empty=False)
        ordered(tuple(k for k, _ in self.diagnostics), 32)


@dataclass(frozen=True, slots=True)
class AnalogueResult(Artifact):
    query: AnalogueQuery
    target_fingerprint: str
    policy_fingerprint: str
    excluded_event_ids: tuple[str, ...]
    pool: tuple[PoolMember, ...]
    scales: tuple[NumericScale, ...]
    candidates: tuple[AnalogueCandidate, ...]
    distribution: ReactionDistribution
    uncertainty: tuple[AnalogueUncertainty, ...]
    diagnostics: PoolDiagnostics
    state: ResultState
    quality: tuple[str, ...]
    engine_version: str = __version__
    schema_version: str = HISTORICAL_ANALOGUE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        digest(self.target_fingerprint)
        digest(self.policy_fingerprint)
        ordered(self.excluded_event_ids, 100000, empty=False)
        ordered(tuple(m.row_id for m in self.pool), 100000)
        ordered(self.quality, 64)
        if (
            self.schema_version != HISTORICAL_ANALOGUE_SCHEMA_VERSION
            or self.query.target_event_id not in self.excluded_event_ids
            or len(self.candidates) > self.query.top_k
            or tuple((c.total_distance, c.event_id, c.revision, c.row_id) for c in self.candidates)
            != tuple(
                sorted(
                    (c.total_distance, c.event_id, c.revision, c.row_id) for c in self.candidates
                )
            )
            or tuple(u.kind for u in self.uncertainty) != tuple(sorted(UncertaintyKind))
        ):
            raise ValidationError("invalid frozen analogue membership/order/uncertainty")

    @property
    def result_id(self) -> str:
        return "analogues_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class MarketReactionSurprise(Artifact):
    analogue_result_id: str
    analogue_fingerprint: str
    target_response_id: str | None
    target_response_fingerprint: str | None
    target_value: float | None
    available_ns: int
    signed_percentile: float | None
    magnitude_percentile: float | None
    signed_residual: float | None
    standardized_mad_residual: float | None
    state: ResultState
    reasons: tuple[str, ...]
    tie_rule: str = "MIDRANK_(LESS+0.5*EQUAL)/N"
    mode: str = "EX_POST_MARKET_REACTION"

    def __post_init__(self) -> None:
        contract(self)
        digest(self.analogue_fingerprint)
        ordered(self.reasons, 64)
        if self.target_response_fingerprint is not None:
            digest(self.target_response_fingerprint)
        if (
            (self.target_response_id is None) != (self.target_response_fingerprint is None)
            or any(
                v is not None and not 0 <= v <= 1
                for v in (self.signed_percentile, self.magnitude_percentile)
            )
            or self.tie_rule != "MIDRANK_(LESS+0.5*EQUAL)/N"
            or self.mode != "EX_POST_MARKET_REACTION"
        ):
            raise ValidationError("invalid ex-post reaction comparison")

    @property
    def reaction_id(self) -> str:
        return "reaction_surprise_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class AnalogueRunManifest(Artifact):
    result_ids: tuple[str, ...]
    result_fingerprints: tuple[str, ...]
    dataset_id: str
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.result_ids, 10000, empty=False)
        if len(self.result_ids) != len(self.result_fingerprints):
            raise ValidationError("run membership mismatch")
        for v in self.result_fingerprints:
            digest(v)

    @property
    def run_id(self) -> str:
        return "analogue_run_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class AnalogueValidation:
    identity: str
    state: ValidationState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64)
