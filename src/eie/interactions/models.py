"""Immutable compact references and explicit finite interaction policies."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import RevisionKind
from eie.events.scope import EventScope
from eie.events.taxonomy import EventType
from eie.evidence.claims import TimeValue, ordered
from eie.graph.taxonomy import RelationshipType
from eie.interactions import EVENT_INTERACTION_SCHEMA_VERSION
from eie.interactions.schema import (
    CollisionType,
    ContaminationCategory,
    DomainState,
    EventInteractionRelation,
    InteractionQuality,
    OrderingBasis,
)
from eie.studies.checks import contract, digest
from eie.studies.market import MarketSession
from eie.studies.models import SessionPolicy
from eie.version import __version__

DAY = 86_400_000_000_000


class Artifact:
    __slots__ = ()

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:interaction:" + type(self).__name__ + ":v1", self)

    @property
    def artifact_id(self) -> str:
        return "interaction_" + type(self).__name__.lower() + "_" + self.fingerprint


def distinct(values: tuple[object, ...], maximum: int) -> None:
    if len(values) > maximum or len(set(values)) != len(values):
        raise ValidationError("bounded distinct interaction members required")


@dataclass(frozen=True, slots=True)
class WindowOverride:
    selector: str
    pre_ns: int
    post_ns: int
    basis: OrderingBasis | None = None

    def __post_init__(self) -> None:
        contract(self)
        if max(self.pre_ns, self.post_ns) > 30 * DAY:
            raise ValidationError("interaction window exceeds hard horizon")


@dataclass(frozen=True, slots=True)
class InteractionWindow(Artifact):
    basis: OrderingBasis = OrderingBasis.EXACT_EVENT_TIME
    pre_ns: int = 600_000_000_000
    post_ns: int = 600_000_000_000
    session_policy: SessionPolicy = SessionPolicy.ELAPSED
    permit_coarse: bool = False
    overrides: tuple[WindowOverride, ...] = ()
    sessions: tuple[MarketSession, ...] = ()
    version: str = "interaction-window-1"

    def __post_init__(self) -> None:
        contract(self)
        if max(self.pre_ns, self.post_ns) > 30 * DAY:
            raise ValidationError("interaction window exceeds hard horizon")
        ordered(tuple(o.selector for o in self.overrides), 128)
        if len(self.sessions) > 1000 or any(
            a.close_ns > b.open_ns for a, b in zip(self.sessions, self.sessions[1:], strict=False)
        ):
            raise ValidationError("bounded nonoverlapping ordered sessions required")
        if self.session_policy is not SessionPolicy.ELAPSED and not self.sessions:
            raise ValidationError("session windows require explicit calendar")

    def basis_for(self, typ: EventType) -> OrderingBasis:
        by_key = {o.selector: o for o in self.overrides}
        selected = by_key.get(typ.code, by_key.get(typ.family.value))
        return self.basis if selected is None or selected.basis is None else selected.basis

    def durations(self, typ: EventType) -> tuple[int, int]:
        by_key = {o.selector: o for o in self.overrides}
        selected = by_key.get(typ.code, by_key.get(typ.family.value))
        return (
            (self.pre_ns, self.post_ns) if selected is None else (selected.pre_ns, selected.post_ns)
        )


@dataclass(frozen=True, slots=True)
class InteractionPolicy(Artifact):
    window: InteractionWindow = InteractionWindow()
    chronology_basis: OrderingBasis = OrderingBasis.SOURCE_AVAILABILITY
    horizon_ns: int = 5 * DAY
    horizon_sessions: int | None = None
    max_pairs: int = 1000
    max_sequence_events: int = 2000
    max_paths: int = 1000
    max_chronology_entries: int = 2000
    max_chains: int = 1
    max_hops: int = 2
    max_output_bytes: int = 10_000_000
    max_run_members: int = 100
    relationship_types: tuple[RelationshipType, ...] = (RelationshipType.SUPPLIER_TO,)
    version: str = "interaction-policy-1"

    def __post_init__(self) -> None:
        contract(self)
        if not 1 <= self.horizon_ns <= 365 * DAY or (
            self.horizon_sessions is not None and not 1 <= self.horizon_sessions <= 100
        ):
            raise ValidationError("finite positive propagation horizon required")
        for n in (
            self.max_pairs,
            self.max_sequence_events,
            self.max_paths,
            self.max_chronology_entries,
            self.max_chains,
            self.max_run_members,
        ):
            if not 1 <= n <= 10000:
                raise ValidationError("finite positive interaction count required")
        if not 1 <= self.max_hops <= 4 or not 4096 <= self.max_output_bytes <= 50_000_000:
            raise ValidationError("interaction hop/byte hard cap exceeded")
        ordered(self.relationship_types, 32)
        if self.chronology_basis not in (
            OrderingBasis.EXACT_EVENT_TIME,
            OrderingBasis.SOURCE_AVAILABILITY,
            OrderingBasis.KNOWLEDGE_TIME,
        ):
            raise ValidationError("propagation requires an explicit precise ordering basis")
        if self.horizon_sessions is not None and not self.window.sessions:
            raise ValidationError("session horizon requires supplied sessions")


@dataclass(frozen=True, slots=True)
class InteractionEvent(Artifact):
    dataset_id: str
    event_id: str
    revision_number: int
    revision_id: str
    event_fingerprint: str
    event_type: EventType
    scope: EventScope
    occurrence: TimeValue
    scheduled: TimeValue
    source_available_ns: int | None
    known_ns: int
    bundle_ids: tuple[str, ...]
    revision_kind: RevisionKind
    thread_id: str
    state: DomainState | LifecycleState
    retracted: bool = False
    quality: frozenset[InteractionQuality] = frozenset()
    timing_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        digest(self.event_fingerprint)
        if self.revision_number < 1 or (
            self.source_available_ns is not None and self.source_available_ns > self.known_ns
        ):
            raise ValidationError("invalid pinned event clocks/revision")
        ordered(self.bundle_ids, 128, empty=False)
        ordered(self.timing_reference_ids, 128)


@dataclass(frozen=True, slots=True)
class PathReference(Artifact):
    graph_id: str
    graph_fingerprint: str
    path_id: str
    node_ids: tuple[str, ...]
    edge_revision_ids: tuple[str, ...]
    relationship_types: tuple[RelationshipType, ...]
    known_ns: int
    valid_ns: int
    qualified: bool = False

    def __post_init__(self) -> None:
        contract(self)
        digest(self.graph_fingerprint)
        if not 1 <= len(self.edge_revision_ids) <= 4 or (
            len(self.node_ids) != len(self.edge_revision_ids) + 1
            or len(set(self.node_ids)) != len(self.node_ids)
            or len(self.relationship_types) != len(self.edge_revision_ids)
        ):
            raise ValidationError("bounded exact simple graph path required")

    @property
    def hop_count(self) -> int:
        return len(self.edge_revision_ids)


@dataclass(frozen=True, slots=True)
class InteractionQuery(Artifact):
    dataset_id: str
    target_event_id: str
    known_as_of_ns: int
    policy_id: str
    graph_id: str | None = None
    event_resolution_id: str | None = None
    collision_types: tuple[CollisionType, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.collision_types, 32)


@dataclass(frozen=True, slots=True)
class EventCollision(Artifact):
    left_row_id: str
    right_row_id: str
    known_as_of_ns: int
    window_id: str
    categories: tuple[CollisionType, ...]
    relations: tuple[EventInteractionRelation, ...]
    left_interval: tuple[int, int] | None
    right_interval: tuple[int, int] | None
    ordering_basis: OrderingBasis
    path_ids: tuple[str, ...] = ()
    quality: frozenset[InteractionQuality] = frozenset()

    def __post_init__(self) -> None:
        contract(self)
        if self.left_row_id >= self.right_row_id:
            raise ValidationError("collision requires canonical distinct symmetric pair")
        ordered(self.categories, 32, empty=False)
        ordered(self.relations, 32, empty=False)
        ordered(self.path_ids, 10000)
        for interval in (self.left_interval, self.right_interval):
            if interval is not None and (interval[0] < 0 or interval[1] < interval[0]):
                raise ValidationError("invalid overlap interval")


@dataclass(frozen=True, slots=True)
class SequenceStep:
    earlier_row_id: str
    later_row_id: str
    elapsed_ns: int
    event_gap: int | None
    knowledge_gap: int
    relation: EventInteractionRelation = EventInteractionRelation.PRECEDES

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.earlier_row_id == self.later_row_id
            or self.elapsed_ns <= 0
            or (self.relation is not EventInteractionRelation.PRECEDES)
        ):
            raise ValidationError("strict sequence step requires positive gap")
        # Secondary-basis gaps can be negative; these are signed differences, not elapsed.


@dataclass(frozen=True, slots=True)
class EventSequence(Artifact):
    dataset_id: str
    entity_id: str
    start_ns: int
    end_ns: int
    known_as_of_ns: int
    basis: OrderingBasis
    row_ids: tuple[str, ...]
    layers: tuple[tuple[str, ...], ...]
    unordered_row_ids: tuple[str, ...]
    steps: tuple[SequenceStep, ...]
    quality: frozenset[InteractionQuality]
    event_resolution_id: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.row_ids, 10000)
        ordered(self.unordered_row_ids, 10000)
        if self.end_ns < self.start_ns or sorted(
            [r for layer in self.layers for r in layer] + list(self.unordered_row_ids)
        ) != list(self.row_ids):
            raise ValidationError("sequence layers must partition exact members")
        for layer in self.layers:
            ordered(layer, 10000, empty=False)
        distinct(self.steps, 10000)


@dataclass(frozen=True, slots=True)
class EventStateTransition(Artifact):
    thread_id: str
    from_row_id: str
    to_row_id: str
    from_state: DomainState | LifecycleState
    to_state: DomainState | LifecycleState
    known_ns: int
    effective: TimeValue
    relation: EventInteractionRelation

    def __post_init__(self) -> None:
        contract(self)
        if self.from_row_id == self.to_row_id or self.relation not in (
            EventInteractionRelation.STATE_TRANSITION_TO,
            EventInteractionRelation.CORRECTION_OF,
            EventInteractionRelation.RETRACTION_OF,
        ):
            raise ValidationError("typed distinct transition source revisions required")


@dataclass(frozen=True, slots=True)
class PropagationChronology(Artifact):
    root_row_id: str
    later_row_id: str
    path_id: str
    basis: OrderingBasis
    root_time_ns: int
    later_time_ns: int
    root_known_ns: int
    later_known_ns: int
    elapsed_ns: int
    quality: frozenset[InteractionQuality] = frozenset()
    description: str = "SUBSEQUENT_RELATED_EVENT"

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.root_row_id == self.later_row_id
            or self.later_time_ns <= self.root_time_ns
            or (self.elapsed_ns != self.later_time_ns - self.root_time_ns)
            or self.description != "SUBSEQUENT_RELATED_EVENT"
        ):
            raise ValidationError("positive descriptive propagation chronology required")


@dataclass(frozen=True, slots=True)
class PropagationChain(Artifact):
    known_as_of_ns: int
    root_row_id: str
    basis: OrderingBasis
    horizon_end_ns: int
    entry_ids: tuple[str, ...]
    layers: tuple[tuple[str, ...], ...]
    quality: frozenset[InteractionQuality] = frozenset()
    truncation_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.entry_ids, 10000)
        if sorted(i for layer in self.layers for i in layer) != list(self.entry_ids):
            raise ValidationError("chain layers must partition chronology entries")
        for layer in self.layers:
            ordered(layer, 10000, empty=False)
        ordered(self.truncation_reasons, 32)


@dataclass(frozen=True, slots=True)
class InteractionContext(Artifact):
    query: InteractionQuery
    target_row_id: str
    member_row_ids: tuple[str, ...]
    collision_ids: tuple[str, ...]
    sequence_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    chronology_ids: tuple[str, ...]
    chain_ids: tuple[str, ...]
    path_ids: tuple[str, ...]
    contamination: tuple[ContaminationCategory, ...]
    counts: tuple[tuple[CollisionType, int], ...]
    quality: frozenset[InteractionQuality]
    blocked_count: int
    schema_version: str = EVENT_INTERACTION_SCHEMA_VERSION
    engine_version: str = __version__

    def __post_init__(self) -> None:
        contract(self)
        for v in (
            self.member_row_ids,
            self.collision_ids,
            self.sequence_ids,
            self.transition_ids,
            self.chronology_ids,
            self.chain_ids,
            self.path_ids,
        ):
            ordered(v, 10000)
        ordered(self.contamination, 32, empty=False)
        if (
            self.target_row_id not in self.member_row_ids
            or self.blocked_count < 1
            or (self.schema_version != EVENT_INTERACTION_SCHEMA_VERSION)
        ):
            raise ValidationError("context requires target, count and supported schema")
        ordered(tuple(k for k, _ in self.counts), 32)
        if any(n < 1 for _, n in self.counts):
            raise ValidationError("descriptive category counts must be positive")


@dataclass(frozen=True, slots=True)
class InteractionRunManifest(Artifact):
    policy_id: str
    known_as_of_ns: int
    context_ids: tuple[str, ...]
    context_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.context_ids, 10000, empty=False)
        if len(self.context_ids) != len(self.context_fingerprints):
            raise ValidationError("run requires exact result fingerprints")
        for value in self.context_fingerprints:
            digest(value)


@dataclass(frozen=True, slots=True)
class InteractionInputManifest(Artifact):
    query: InteractionQuery
    target_row_id: str
    collision_row_ids: tuple[str, ...]
    sequence_row_ids: tuple[str, ...]
    transition_row_ids: tuple[str, ...]
    propagation_row_ids: tuple[str, ...]
    paths: tuple[PathReference, ...]
    identity_groups: tuple[tuple[str, ...], ...]
    identity_fingerprint: str | None

    def __post_init__(self) -> None:
        contract(self)
        for ids in (
            self.collision_row_ids,
            self.sequence_row_ids,
            self.transition_row_ids,
            self.propagation_row_ids,
        ):
            ordered(ids, 10000)
        ordered(tuple(p.artifact_id for p in self.paths), 10000)
        if self.identity_fingerprint is not None:
            digest(self.identity_fingerprint)
        for group in self.identity_groups:
            ordered(group, 1000, empty=False)
        distinct(self.identity_groups, 10000)
