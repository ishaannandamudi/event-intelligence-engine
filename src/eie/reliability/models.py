"""Frozen bounded reference artifacts; source content is never embedded."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import TimeValue, ordered
from eie.reliability import SOURCE_RELIABILITY_SCHEMA_VERSION
from eie.reliability.schema import (
    ClockKind,
    CoverageState,
    LatencyQuality,
    ReliabilityQuality,
    RevisionBehavior,
    SubjectType,
    WindowKind,
)
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract, digest
from eie.version import __version__

DAY = 86_400_000_000_000


class Artifact:
    def __post_init__(self) -> None:
        contract(self)

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:reliability:" + type(self).__name__ + ":v1", self)

    @property
    def artifact_id(self) -> str:
        return "srel_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class InformationClocks(Artifact):
    valid: TimeValue | None = None
    publication: TimeValue | None = None
    source_available: TimeValue | None = None
    observed: TimeValue | None = None
    ingested: TimeValue | None = None
    normalized: TimeValue | None = None
    snapshot: TimeValue | None = None
    coarse_timezone: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        if self.coarse_timezone is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(self.coarse_timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValidationError("explicit source timezone required") from exc

    def get(self, kind: ClockKind) -> TimeValue | None:
        return {
            ClockKind.VALID_TIME: self.valid,
            ClockKind.PUBLICATION_TIME: self.publication,
            ClockKind.SOURCE_AVAILABLE_TIME: self.source_available,
            ClockKind.OBSERVED_TIME: self.observed,
            ClockKind.INGESTED_TIME: self.ingested,
            ClockKind.NORMALIZED_TIME: self.normalized,
            ClockKind.SNAPSHOT_AVAILABLE_TIME: self.snapshot,
        }[kind]


@dataclass(frozen=True, slots=True)
class ProcessingTiming(Artifact):
    """Explicit trusted instrumentation; missing clocks are not inferred from cutoffs."""

    subject_id: str
    source_fingerprint: str
    evidence_reference: str
    available_ns: int
    normalized: TimeValue | None = None
    snapshot: TimeValue | None = None
    coarse_timezone: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.source_fingerprint)
        InformationClocks(coarse_timezone=self.coarse_timezone)
        if self.normalized is None and self.snapshot is None:
            raise ValidationError("processing timing requires an explicit clock")
        for clock in (self.normalized, self.snapshot):
            if (
                clock is not None
                and clock.timestamp_ns is not None
                and (clock.timestamp_ns > self.available_ns)
            ):
                raise ValidationError("processing clock follows evidence availability")


@dataclass(frozen=True, slots=True)
class SourceHistoryRow(Artifact):
    dataset_id: str
    kind: SubjectType
    subject_id: str
    thread_id: str
    source_revision_id: str
    source_fingerprint: str
    raw_id: str
    lineage_id: str | None
    source: SourceIdentity
    resource_id: str
    source_family: str
    event_type: str
    field: str
    scope_id: str
    number: int
    known_ns: int
    metric_available_ns: int
    sample_available_ns: int
    clocks: InformationClocks
    behavior: RevisionBehavior
    value_fingerprint: str
    valid_to_ns: int | None = None
    previous_source_id: str | None = None
    timing_id: str | None = None
    reconstructed: bool = False
    active: bool = True
    content_time: TimeValue | None = None

    def __post_init__(self) -> None:
        contract(self)
        for v in (self.source_fingerprint, self.value_fingerprint):
            digest(v)
        if (
            not 1 <= self.number <= 10000
            or not self.known_ns <= self.sample_available_ns <= self.metric_available_ns
        ):
            raise ValidationError("bounded source revision and actual metric availability required")
        if self.valid_to_ns is not None and (
            self.clocks.valid is None
            or self.clocks.valid.timestamp_ns is None
            or self.valid_to_ns <= self.clocks.valid.timestamp_ns
        ):
            raise ValidationError("explicit positive half-open validity required")


@dataclass(frozen=True, slots=True)
class LatencyComponent(Artifact):
    start: ClockKind
    end: ClockKind
    delta: int | None
    lower: int | None
    upper: int | None
    quality: frozenset[LatencyQuality]

    def __post_init__(self) -> None:
        contract(self)
        if self.start is self.end or (self.lower is None) != (self.upper is None):
            raise ValidationError("distinct clocks and paired interval bounds required")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValidationError("ordered signed latency bounds required")
        if self.delta is not None and (self.lower, self.upper) != (self.delta, self.delta):
            raise ValidationError("exact latency needs identical bounds")


@dataclass(frozen=True, slots=True)
class InformationLatency(Artifact):
    row_id: str
    known_as_of_ns: int
    clocks: InformationClocks
    components: tuple[LatencyComponent, ...]
    missing_clocks: tuple[ClockKind, ...]
    quality: frozenset[LatencyQuality]


@dataclass(frozen=True, slots=True)
class AuditEntry(Artifact):
    row_id: str
    source_revision_id: str
    thread_id: str
    number: int
    known_ns: int
    metric_available_ns: int
    valid: TimeValue | None
    valid_to_ns: int | None
    behavior: RevisionBehavior
    observed: TimeValue | None


@dataclass(frozen=True, slots=True)
class BitemporalAuditResult(Artifact):
    dataset_id: str
    subject_id: str
    known_as_of_ns: int
    valid_at_ns: int
    entries: tuple[AuditEntry, ...]
    selected_row_ids: tuple[str, ...]
    unknown_validity_row_ids: tuple[str, ...]
    quality: frozenset[ReliabilityQuality]

    def __post_init__(self) -> None:
        contract(self)
        if len(self.entries) > 10000:
            raise ValidationError("audit history bound exceeded")
        ordered(self.selected_row_ids, 10000)
        ordered(self.unknown_validity_row_ids, 10000)


@dataclass(frozen=True, slots=True)
class SourceReliabilityPolicy(Artifact):
    window: WindowKind = WindowKind.ALL_PRIOR
    days: int | None = None
    observations: int | None = None
    minimum_count: int = 10
    quantiles: tuple[float, ...] = (0.25, 0.5, 0.75, 0.95)
    required_clocks: tuple[ClockKind, ...] = (ClockKind.OBSERVED_TIME, ClockKind.PUBLICATION_TIME)
    max_observations: int = 10000
    max_revisions: int = 1000
    max_fields: int = 100
    max_groups: int = 100
    max_bytes: int = 5_000_000
    max_run_members: int = 100
    conflict_policy: str = "EXACT_VALUE_DISAGREEMENT_NO_GROUND_TRUTH"
    correction_definition: str = "EXPLICIT_OWNER_ACTION"
    version: str = "1"

    def __post_init__(self) -> None:
        contract(self)
        if (self.window is WindowKind.TRAILING_DAYS) != (self.days is not None) or (
            (self.window is WindowKind.TRAILING_OBSERVATIONS) != (self.observations is not None)
        ):
            raise ValidationError("explicit window parameters required")
        if self.days is not None and not 1 <= self.days <= 36500:
            raise ValidationError("trailing day window bound exceeded")
        if self.observations is not None and not 1 <= self.observations <= self.max_observations:
            raise ValidationError("trailing observation window bound exceeded")
        for n, maximum in (
            (self.minimum_count, 10000),
            (self.max_observations, 100000),
            (self.max_revisions, 10000),
            (self.max_fields, 1000),
            (self.max_groups, 1000),
            (self.max_bytes, 100_000_000),
            (self.max_run_members, 1000),
        ):
            if not 1 <= n <= maximum:
                raise ValidationError("reliability count/byte bound exceeded")
        if (
            not self.quantiles
            or len(self.quantiles) > 20
            or (
                self.quantiles != tuple(sorted(set(self.quantiles)))
                or any(not 0 <= q <= 1 for q in self.quantiles)
            )
        ):
            raise ValidationError("bounded ordered quantile probabilities required")
        if self.required_clocks != tuple(sorted(set(self.required_clocks), key=lambda c: c.value)):
            raise ValidationError("ordered distinct clock requirements required")
        if self.conflict_policy != "EXACT_VALUE_DISAGREEMENT_NO_GROUND_TRUTH" or (
            self.correction_definition != "EXPLICIT_OWNER_ACTION"
        ):
            raise ValidationError("unsupported metric definition")


@dataclass(frozen=True, slots=True)
class ReliabilityQuery(Artifact):
    dataset_id: str
    provider: str
    resource_id: str
    known_as_of_ns: int
    policy: SourceReliabilityPolicy
    event_type: str | None = None
    field: str | None = None
    scope_id: str | None = None
    kind: SubjectType = SubjectType.CLAIM
    coverage_universe_id: str | None = None


@dataclass(frozen=True, slots=True)
class RateMetric(Artifact):
    name: str
    count: int
    denominator: int
    ratio: float | None

    def __post_init__(self) -> None:
        contract(self)
        if not 0 <= self.count <= self.denominator or self.ratio != (
            None if self.denominator == 0 else self.count / self.denominator
        ):
            raise ValidationError("rate denominator/count disagreement")


@dataclass(frozen=True, slots=True)
class LatencyDistribution(Artifact):
    start: ClockKind
    end: ClockKind
    count: int
    missing_count: int
    coarse_count: int
    skew_count: int
    median: float | None
    mean: float | None
    mad: float | None
    minimum: int | None
    maximum: int | None
    quantiles: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class CoverageSlot(Artifact):
    key: str
    due_ns: int
    event_type: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class CoverageUniverse(Artifact):
    dataset_id: str
    provider: str
    resource_id: str
    scope_id: str
    start_ns: int
    end_ns: int
    known_ns: int
    denominator_reference: str
    denominator_fingerprint: str
    slots: tuple[CoverageSlot, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        contract(self)
        digest(self.denominator_fingerprint)
        if self.start_ns >= self.end_ns or len(self.slots) > 100000:
            raise ValidationError("bounded explicit coverage interval required")
        keys = tuple(s.key for s in self.slots)
        if keys != tuple(sorted(set(keys))) or any(
            not self.start_ns <= s.due_ns < self.end_ns for s in self.slots
        ):
            raise ValidationError("distinct ordered expected slots within universe required")


@dataclass(frozen=True, slots=True)
class CoverageAssessment(Artifact):
    universe_id: str
    slot_key: str
    known_ns: int
    state: CoverageState
    evidence_reference: str
    evidence_fingerprint: str
    row_id: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.evidence_fingerprint)
        if (self.state is CoverageState.OBSERVED) != (self.row_id is not None):
            raise ValidationError("only observed coverage pins a source row")


@dataclass(frozen=True, slots=True)
class SourceCoverageResult(Artifact):
    universe_id: str | None
    known_as_of_ns: int
    expected_count: int | None
    observed_count: int
    missing_count: int
    known_empty_count: int
    unknown_count: int
    not_applicable_count: int
    ratio: float | None
    assessment_ids: tuple[str, ...]
    quality: frozenset[ReliabilityQuality]


@dataclass(frozen=True, slots=True)
class SourceReliabilityResult(Artifact):
    query: ReliabilityQuery
    input_row_ids: tuple[str, ...]
    sample_row_ids: tuple[str, ...]
    peer_row_ids: tuple[str, ...]
    sample_count: int
    source_classes: tuple[str, ...]
    behavior: tuple[RateMetric, ...]
    consistency: tuple[RateMetric, ...]
    corroboration: RateMetric
    conflict: RateMetric
    timeliness: tuple[LatencyDistribution, ...]
    coverage_id: str | None
    quality: frozenset[ReliabilityQuality]
    schema_version: str = SOURCE_RELIABILITY_SCHEMA_VERSION
    engine_version: str = __version__

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.sample_row_ids, self.query.policy.max_observations)
        ordered(self.peer_row_ids, self.query.policy.max_observations)
        if self.sample_count != len(self.sample_row_ids) or (
            self.schema_version != SOURCE_RELIABILITY_SCHEMA_VERSION
        ):
            raise ValidationError("reliability sample/schema disagreement")


@dataclass(frozen=True, slots=True)
class ReliabilityRunManifest(Artifact):
    known_as_of_ns: int
    result_ids: tuple[str, ...]
    result_fingerprints: tuple[str, ...]
    policy_id: str


@dataclass(frozen=True, slots=True)
class ComponentDelta(Artifact):
    component: str
    left: float | None
    right: float | None
    difference: float | None


@dataclass(frozen=True, slots=True)
class SourceComparison(Artifact):
    left_id: str
    right_id: str
    deltas: tuple[ComponentDelta, ...]
