"""Immutable, bounded schema 1.0 artifacts. Evidence bodies remain in their owners."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import ordered
from eie.expectations.units import CanonicalUnit
from eie.intelligence import EVENT_INTELLIGENCE_SCHEMA_VERSION
from eie.intelligence.schema import (
    Importance,
    IntelligenceMode,
    MaterialityCategory,
    MaterialityKind,
    NoveltyDimension,
    NoveltyState,
    ReferenceKind,
    UncertaintyKind,
    UncertaintyState,
    ValidationState,
)
from eie.studies.checks import contract, digest
from eie.version import __version__


@dataclass(frozen=True, slots=True)
class InputReference:
    kind: ReferenceKind
    identity: str
    fingerprint: str
    known_ns: int

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)


@dataclass(frozen=True, slots=True)
class IntelligenceRequest:
    event_id: str
    revision_number: int
    dataset_id: str
    known_as_of_ns: int
    available_ns: int
    policy_id: str
    event_resolution_id: str | None = None
    prior_graph_id: str | None = None
    current_graph_id: str | None = None
    scale_assertion_id: str | None = None
    guidance_ids: tuple[str, ...] = ()
    surprise_ids: tuple[str, ...] = ()
    impact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if self.revision_number < 1 or self.available_ns < self.known_as_of_ns:
            raise ValidationError("intelligence revision/availability invalid")
        for values in (self.guidance_ids, self.surprise_ids, self.impact_ids):
            ordered(values, 128)


@dataclass(frozen=True, slots=True)
class IntelligenceInputManifest:
    request: IntelligenceRequest
    dataset_fingerprint: str
    build_id: str | None
    current: tuple[InputReference, ...]
    prior: tuple[InputReference, ...]
    context: tuple[InputReference, ...]
    excluded_event_ids: tuple[str, ...]
    schema_version: str = EVENT_INTELLIGENCE_SCHEMA_VERSION
    engine_version: str = __version__

    def __post_init__(self) -> None:
        contract(self)
        digest(self.dataset_fingerprint)
        ordered(self.excluded_event_ids, 1000, empty=False)
        if self.request.event_id not in self.excluded_event_ids:
            raise ValidationError("current event must be excluded from independent distributions")
        if self.schema_version != EVENT_INTELLIGENCE_SCHEMA_VERSION:
            raise ValidationError("unsupported event intelligence schema")
        for refs in (self.current, self.prior, self.context):
            keys = tuple((r.kind.value, r.identity) for r in refs)
            if len(refs) > 100000 or keys != tuple(sorted(set(keys))):
                raise ValidationError("bounded ordered distinct input references required")
        if any(r.known_ns >= self.request.known_as_of_ns for r in self.prior) or any(
            r.known_ns > self.request.known_as_of_ns for r in (*self.current, *self.context)
        ):
            raise ValidationError("strict prior/context knowledge cutoff violated")

    @property
    def input_fingerprint(self) -> str:
        return fingerprint("eie:intelligence-input:v1", self)

    @property
    def manifest_id(self) -> str:
        return "intel_inputs_" + self.input_fingerprint


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    value: float | None
    unit: CanonicalUnit | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        if (self.value is None) != (self.reason is not None):
            raise ValidationError("unavailable metric requires an explicit reason")


@dataclass(frozen=True, slots=True)
class NoveltyComponent:
    dimension: NoveltyDimension
    subject_id: str
    state: NoveltyState
    current_ids: tuple[str, ...]
    prior_ids: tuple[str, ...]
    metrics: tuple[Metric, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        allowed = {
            NoveltyDimension.DOCUMENT: {
                NoveltyState.NEW_DOCUMENT,
                NoveltyState.EXACT_REPEAT,
                NoveltyState.PARTIAL_REPEAT,
                NoveltyState.NO_BASELINE,
            },
            NoveltyDimension.FACTUAL: {
                NoveltyState.NEW_FACT,
                NoveltyState.UPDATED_FACT,
                NoveltyState.CORRECTION,
                NoveltyState.CONFIRMATION,
                NoveltyState.RETRACTION,
                NoveltyState.RESTATEMENT,
                NoveltyState.KNOWN_ALREADY,
                NoveltyState.UNRESOLVED,
            },
            NoveltyDimension.NUMERIC: {
                NoveltyState.COMPARABLE,
                NoveltyState.NO_BASELINE,
                NoveltyState.UNRESOLVED,
            },
            NoveltyDimension.GUIDANCE: {
                NoveltyState.INITIATED,
                NoveltyState.WITHDRAWN,
                NoveltyState.UNRESOLVED,
                NoveltyState.KNOWN_ALREADY,
                NoveltyState.CHANGED,
                NoveltyState.NO_BASELINE,
            },
            NoveltyDimension.ENTITY: {
                NoveltyState.NEW_ENTITY,
                NoveltyState.KNOWN_ENTITY,
                NoveltyState.UNRESOLVED,
            },
            NoveltyDimension.CROSS_SOURCE: {
                NoveltyState.NEW_PRIMARY_INFORMATION,
                NoveltyState.NEW_SECONDARY_INFORMATION,
                NoveltyState.INDEPENDENT_CONFIRMATION,
                NoveltyState.SYNDICATED_REPEAT,
                NoveltyState.CONTRADICTION,
                NoveltyState.CORRECTION,
                NoveltyState.NO_NEW_INFORMATION,
            },
        }
        if self.state not in allowed[self.dimension]:
            raise ValidationError("novelty state belongs to another dimension")
        ordered(self.current_ids, 10000)
        ordered(self.prior_ids, 100000)
        ordered(self.reasons, 64)
        names = tuple(m.name for m in self.metrics)
        if len(names) > 32 or names != tuple(sorted(set(names))):
            raise ValidationError("bounded ordered distinct novelty metrics required")

    @property
    def component_id(self) -> str:
        return "novelty_" + fingerprint("eie:novelty-component:v1", self)


@dataclass(frozen=True, slots=True)
class StaticImportance:
    category: Importance
    rule: str
    policy_fingerprint: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.policy_fingerprint)


@dataclass(frozen=True, slots=True)
class MaterialityComponent:
    kind: MaterialityKind
    subject_id: str
    category: MaterialityCategory
    metrics: tuple[Metric, ...]
    input_ids: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.input_ids, 10000)
        ordered(self.reasons, 64)
        names = tuple(m.name for m in self.metrics)
        if len(names) > 16 or names != tuple(sorted(set(names))):
            raise ValidationError("bounded ordered distinct materiality metrics required")


@dataclass(frozen=True, slots=True)
class EventMateriality:
    category: MaterialityCategory
    components: tuple[MaterialityComponent, ...]
    rule: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64)
        keys = tuple((c.kind.value, c.subject_id) for c in self.components)
        if len(keys) > 512 or keys != tuple(sorted(set(keys))):
            raise ValidationError("bounded ordered distinct materiality components required")


@dataclass(frozen=True, slots=True)
class UncertaintyComponent:
    kind: UncertaintyKind
    state: UncertaintyState
    reasons: tuple[str, ...]
    input_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64, empty=False)
        ordered(self.input_ids, 10000)
        readiness = {
            UncertaintyState.NOT_COMPUTED,
            UncertaintyState.INSUFFICIENT_INPUTS,
            UncertaintyState.READY_FOR_ANALOGUE_SEARCH,
        }
        if (
            self.kind is UncertaintyKind.ANALOGUE
            and self.state not in readiness
            or (
                self.kind is not UncertaintyKind.ANALOGUE
                and self.state in readiness - {UncertaintyState.NOT_COMPUTED}
            )
        ):
            raise ValidationError("analogue readiness cannot masquerade as confidence")


@dataclass(frozen=True, slots=True)
class EventIntelligenceResult:
    manifest: IntelligenceInputManifest
    event_revision_id: str
    event_type: str
    event_family: str
    entity_ids: tuple[str, ...]
    policy_fingerprint: str
    novelty: tuple[NoveltyComponent, ...]
    static_importance: StaticImportance
    materiality: EventMateriality
    uncertainty: tuple[UncertaintyComponent, ...]
    quality: tuple[str, ...]
    mode: IntelligenceMode = IntelligenceMode.EX_ANTE_INTELLIGENCE

    def __post_init__(self) -> None:
        contract(self)
        digest(self.policy_fingerprint)
        ordered(self.entity_ids, 1000)
        ordered(self.quality, 64)
        keys = tuple((c.dimension.value, c.subject_id) for c in self.novelty)
        if len(keys) > 10000 or keys != tuple(sorted(set(keys))):
            raise ValidationError("bounded ordered distinct novelty components required")
        if {c.dimension for c in self.novelty} != set(NoveltyDimension):
            raise ValidationError("all six independent novelty dimensions required")
        if tuple(c.kind for c in self.uncertainty) != tuple(sorted(UncertaintyKind)):
            raise ValidationError("all eight independent uncertainty components required")
        expected_mode = (
            IntelligenceMode.EX_POST_AUDIT
            if self.manifest.request.impact_ids
            else IntelligenceMode.EX_ANTE_INTELLIGENCE
        )
        if (
            self.mode is not expected_mode
            or self.static_importance.policy_fingerprint != self.policy_fingerprint
        ):
            raise ValidationError("intelligence mode/importance policy references disagree")

    @property
    def event_id(self) -> str:
        return self.manifest.request.event_id

    @property
    def baseline_cutoff_ns(self) -> int:
        return self.manifest.request.known_as_of_ns

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:event-intelligence:v1", self)

    @property
    def intelligence_id(self) -> str:
        return "intelligence_" + self.result_fingerprint


@dataclass(frozen=True, slots=True)
class IntelligenceAudit:
    intelligence_id: str
    impact_ids: tuple[str, ...]
    impact_fingerprints: tuple[str, ...]
    impact_uncertainty: UncertaintyComponent
    available_ns: int
    mode: IntelligenceMode = IntelligenceMode.EX_POST_AUDIT

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.impact_ids, 128, empty=False)
        if len(self.impact_ids) != len(self.impact_fingerprints) or (
            self.impact_uncertainty.kind is not UncertaintyKind.IMPACT
            or self.mode is not IntelligenceMode.EX_POST_AUDIT
        ):
            raise ValidationError("separate ex-post impact audit required")
        for value in self.impact_fingerprints:
            digest(value)

    @property
    def audit_id(self) -> str:
        return "intel_audit_" + fingerprint("eie:intelligence-audit:v1", self)


@dataclass(frozen=True, slots=True)
class IntelligenceRunManifest:
    intelligence_ids: tuple[str, ...]
    result_fingerprints: tuple[str, ...]
    dataset_id: str
    policy_id: str
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.intelligence_ids, 10000, empty=False)
        if len(self.intelligence_ids) != len(self.result_fingerprints):
            raise ValidationError("intelligence run membership disagrees")
        for value in self.result_fingerprints:
            digest(value)

    @property
    def run_id(self) -> str:
        return "intel_run_" + fingerprint("eie:intelligence-run:v1", self)


@dataclass(frozen=True, slots=True)
class IntelligenceValidation:
    intelligence_id: str
    state: ValidationState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.reasons, 64)
