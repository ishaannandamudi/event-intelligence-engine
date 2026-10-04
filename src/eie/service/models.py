"""Immutable compact contracts. Operational clocks and IDs are separate."""

from dataclasses import dataclass, field

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState
from eie.entities.models import ISSUER_ID, SECURITY_ID
from eie.events.lifecycle import LifecycleState
from eie.events.scope import EventScope, ScopeKind
from eie.evidence.claims import TimeValue, ordered
from eie.expectations.surprise import SurpriseObservation
from eie.reliability.models import InformationClocks
from eie.service import API_VERSION, SNAPSHOT_SCHEMA_VERSION
from eie.service.schema import (
    CompletenessState,
    ComponentKind,
    ComponentState,
    EvaluationAxis,
    OwnerKind,
    ReadinessState,
    ServiceCode,
    ValidationDepth,
)
from eie.studies.checks import contract, digest
from eie.version import __version__

ALL_COMPONENTS = tuple(sorted(ComponentKind))


class Artifact:
    def __post_init__(self) -> None:
        contract(self)

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:service:" + type(self).__name__ + ":v1", self)

    @property
    def artifact_id(self) -> str:
        return "eies_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class SnapshotLimits(Artifact):
    max_components: int = 19
    max_references: int = 256
    max_identity_rows: int = 10000
    max_snapshot_bytes: int = 1_000_000
    max_expansion_bytes: int = 5_000_000
    max_batch: int = 500
    max_batch_bytes: int = 20_000_000
    max_request_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        contract(self)
        if any(
            not 1 <= getattr(self, name) <= ceiling
            for name, ceiling in (
                ("max_components", 19),
                ("max_references", 10000),
                ("max_identity_rows", 20000),
                ("max_snapshot_bytes", 10_000_000),
                ("max_expansion_bytes", 20_000_000),
                ("max_batch", 1000),
                ("max_batch_bytes", 100_000_000),
                ("max_request_bytes", 5_000_000),
            )
        ):
            raise ValidationError("bounded service limits required")


DEFAULT_LIMITS = SnapshotLimits()


@dataclass(frozen=True, slots=True)
class SnapshotRequest(Artifact):
    catalog_id: str
    scope: EventScope
    known_as_of_ns: int
    valid_at_ns: int | None = None
    exchange: str | None = None
    components: tuple[ComponentKind, ...] = ALL_COMPONENTS
    required: tuple[ComponentKind, ...] = ()
    policy_ids: tuple[str, ...] = ()
    build_ids: tuple[str, ...] = ()
    limits: SnapshotLimits = DEFAULT_LIMITS
    schema_version: str = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.components, self.limits.max_components)
        ordered(self.required, self.limits.max_components)
        ordered(self.policy_ids, 128)
        ordered(self.build_ids, 128)
        if not self.components or not set(self.required) <= set(self.components):
            raise ValidationError("nonempty selection and selected required components required")
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION or len(self.scope.identifiers) > 1:
            raise ValidationError("supported schema and one explicit scope identifier required")
        if self.scope.kind is ScopeKind.SYMBOL:
            from eie.entities.sec import EXCHANGE, TICKER

            if (
                self.exchange is None
                or not EXCHANGE.fullmatch(self.exchange)
                or not TICKER.fullmatch(self.scope.identifiers[0])
            ):
                raise ValidationError("symbol request requires exact ticker and exchange")
        elif self.exchange is not None:
            raise ValidationError("exchange is only applicable to symbol scope")
        if self.scope.kind is ScopeKind.COMPANY and not ISSUER_ID.fullmatch(
            self.scope.identifiers[0]
        ):
            raise ValidationError("company scope requires stable issuer identity")
        if self.scope.kind is ScopeKind.SECURITY and not SECURITY_ID.fullmatch(
            self.scope.identifiers[0]
        ):
            raise ValidationError("security scope requires stable security identity")

    @property
    def valid_ns(self) -> int:
        return self.known_as_of_ns if self.valid_at_ns is None else self.valid_at_ns


@dataclass(frozen=True, slots=True)
class ProvenancePin(Artifact):
    kind: str
    identity: str
    fingerprint: str = field()
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)


@dataclass(frozen=True, slots=True)
class EventSummary(Artifact):
    event_id: str
    revision_id: str
    revision_number: int
    event_type: str
    scope: EventScope
    lifecycle: LifecycleState
    confirmation: ConfirmationState
    clocks: InformationClocks
    scheduled_for_ns: int | None
    source_classes: tuple[str, ...]
    quality: tuple[str, ...]
    scheduled_time: TimeValue | None = None


@dataclass(frozen=True, slots=True)
class ArtifactReference(Artifact):
    kind: OwnerKind
    owner_id: str
    fingerprint: str = field()
    known_ns: int
    available_ns: int
    schema_version: str
    provenance: tuple[ProvenancePin, ...]
    lookup: tuple[str, ...]
    storage_fingerprint: str
    event: EventSummary | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)
        digest(self.storage_fingerprint)
        if not 1 <= len(self.lookup) <= 2:
            raise ValidationError("bounded native owner locator required")
        if any(p.available_ns > self.available_ns for p in self.provenance):
            raise ValidationError("reference includes unavailable provenance")
        if self.known_ns > self.available_ns or self.schema_version != "1.0":
            raise ValidationError("reference availability/schema invalid")


@dataclass(frozen=True, slots=True)
class Publication(Artifact):
    catalog_id: str
    component: ComponentKind
    reference: ArtifactReference
    group_key: str
    ordinal: int
    scopes: tuple[EventScope, ...]
    state: ComponentState = ComponentState.AVAILABLE
    quality: tuple[str, ...] = ()
    valid_at_ns: int | None = None

    def __post_init__(self) -> None:
        contract(self)
        if not 0 <= self.ordinal <= 10000 or not self.scopes or len(self.scopes) > 256:
            raise ValidationError("bounded publication scope/revision required")
        ordered(self.quality, 128)
        keys = tuple((s.kind.value, s.identifiers) for s in self.scopes)
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("publication scopes must be ordered and unique")


@dataclass(frozen=True, slots=True)
class CoverageDeclaration(Artifact):
    catalog_id: str
    component: ComponentKind
    scope: EventScope
    available_ns: int
    completeness: CompletenessState
    evidence: ProvenancePin

    def __post_init__(self) -> None:
        contract(self)
        if self.evidence.available_ns > self.available_ns:
            raise ValidationError("coverage declaration cannot precede evidence")


@dataclass(frozen=True, slots=True)
class IdentityBinding(Artifact):
    issuer_id: str | None = None
    security_id: str | None = None
    listing_id: str | None = None
    reference_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotComponent(Artifact):
    kind: ComponentKind
    state: ComponentState
    completeness: CompletenessState
    references: tuple[ArtifactReference, ...]
    publication_ids: tuple[str, ...]
    coverage_id: str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.publication_ids, 10000)
        ordered(self.reasons, 128)
        if len(self.references) != len(self.publication_ids):
            raise ValidationError("component reference membership mismatch")
        if self.state is ComponentState.UNAVAILABLE and self.references:
            raise ValidationError("unavailable component cannot contain references")
        if self.state is ComponentState.NOT_APPLICABLE and (
            self.references or self.completeness is not CompletenessState.NOT_APPLICABLE
        ):
            raise ValidationError("not-applicable component must be empty and qualified")


@dataclass(frozen=True, slots=True)
class SnapshotCompleteness(Artifact):
    dimensions: tuple[tuple[ComponentKind, CompletenessState], ...]

    def __post_init__(self) -> None:
        contract(self)
        if tuple(k for k, _ in self.dimensions) != tuple(sorted({k for k, _ in self.dimensions})):
            raise ValidationError("ordered distinct completeness dimensions required")


@dataclass(frozen=True, slots=True)
class EIESnapshot(Artifact):
    request: SnapshotRequest
    identity: IdentityBinding
    components: tuple[SnapshotComponent, ...]
    completeness: SnapshotCompleteness
    provenance: tuple[ProvenancePin, ...]
    versions: tuple[tuple[str, str], ...]
    engine_version: str = __version__
    schema_version: str = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        contract(self)
        if tuple(
            c.kind for c in self.components
        ) != self.request.components or self.completeness.dimensions != tuple(
            (c.kind, c.completeness) for c in self.components
        ):
            raise ValidationError("snapshot selection/completeness mismatch")
        if self.engine_version != __version__ or self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValidationError("unsupported final snapshot version")
        from eie.service.compatibility import check_versions

        check_versions(self.versions)
        if any(len(c.references) > self.request.limits.max_references for c in self.components):
            raise ValidationError("snapshot component reference bound exceeded")
        cutoff = self.request.known_as_of_ns
        if any(r.available_ns > cutoff for c in self.components for r in c.references) or any(
            p.available_ns > cutoff for p in self.provenance
        ):
            raise ValidationError("snapshot includes future evidence")

    @property
    def snapshot_id(self) -> str:
        return self.artifact_id


@dataclass(frozen=True, slots=True)
class ServiceFailure(Artifact):
    code: ServiceCode
    engine_version: str = __version__
    api_version: str = API_VERSION


@dataclass(frozen=True, slots=True)
class BatchItem(Artifact):
    position: int
    request_fingerprint: str
    snapshot: EIESnapshot | None = None
    failure: ServiceFailure | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.request_fingerprint)
        if self.position < 0 or (self.snapshot is None) == (self.failure is None):
            raise ValidationError("batch item requires one outcome")


@dataclass(frozen=True, slots=True)
class BatchResult(Artifact):
    items: tuple[BatchItem, ...]
    engine_version: str = __version__
    api_version: str = API_VERSION

    def __post_init__(self) -> None:
        contract(self)
        if tuple(i.position for i in self.items) != tuple(range(len(self.items))):
            raise ValidationError("batch positions must preserve input order")
        if self.engine_version != __version__ or self.api_version != API_VERSION:
            raise ValidationError("unsupported batch response version")


@dataclass(frozen=True, slots=True)
class HealthResult(Artifact):
    alive: bool = True
    engine_version: str = __version__
    api_version: str = API_VERSION


@dataclass(frozen=True, slots=True)
class ReadinessResult(Artifact):
    state: ReadinessState
    reasons: tuple[str, ...]
    historical_queries: bool
    live_ingestion: bool | None
    engine_version: str = __version__
    api_version: str = API_VERSION


@dataclass(frozen=True, slots=True)
class ValidationResult(Artifact):
    snapshot_id: str
    depth: ValidationDepth
    valid: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationDimension(Artifact):
    axis: EvaluationAxis
    checks: tuple[tuple[str, str], ...]
    reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationResult(Artifact):
    snapshot_id: str
    dimensions: tuple[EvaluationDimension, ...]


@dataclass(frozen=True, slots=True)
class BackupManifest(Artifact):
    file_fingerprint: str
    store_version: str
    snapshot_count: int
    publication_count: int

    def __post_init__(self) -> None:
        contract(self)
        digest(self.file_fingerprint)


@dataclass(frozen=True, slots=True)
class RetainedExpectation(Artifact):
    namespace: str
    provider_id: str | None
    value: SurpriseObservation
