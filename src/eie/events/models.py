"""Canonical event revisions, distinct from raw observations."""

from dataclasses import dataclass
from math import isfinite

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.schema import EVENT_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import EventRevision, RevisionKind
from eie.sources.trust import SourceClass

FactScalar = str | bool | int | float | None


@dataclass(frozen=True, slots=True)
class StructuredFact:
    name: str
    value: FactScalar
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValidationError("fact name must be nonempty and trimmed")
        if self.unit is not None and (not self.unit or self.unit != self.unit.strip()):
            raise ValidationError("invalid fact unit")
        if isinstance(self.value, float) and not isfinite(self.value):
            raise ValidationError("non-finite fact value")
        if self.value is not None and not isinstance(self.value, (str, bool, int, float)):
            raise ValidationError("fact value must be a JSON scalar")


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    identity: EventIdentity
    revision: EventRevision
    lifecycle: LifecycleState
    confirmation: ConfirmationState
    evidence_observation_ids: tuple[str, ...]
    source_classes: frozenset[SourceClass]
    title: str
    scheduled_for_ns: int | None = None
    announced_at_ns: int | None = None
    published_at_ns: int | None = None
    source_observed_at_ns: int | None = None
    ingested_at_ns: int | None = None
    effective_at_ns: int | None = None
    last_updated_at_ns: int | None = None
    facts: tuple[StructuredFact, ...] = ()
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, EVENT_SCHEMA_VERSION)
        if (
            not isinstance(self.identity, EventIdentity)
            or not isinstance(self.revision, EventRevision)
            or not isinstance(self.lifecycle, LifecycleState)
            or not isinstance(self.confirmation, ConfirmationState)
            or not isinstance(self.evidence_observation_ids, tuple)
            or not isinstance(self.source_classes, frozenset)
            or not isinstance(self.facts, tuple)
            or not isinstance(self.quality_flags, frozenset)
        ):
            raise ValidationError("typed immutable event fields required")
        if (
            any(not isinstance(item, str) for item in self.evidence_observation_ids)
            or any(not isinstance(item, SourceClass) for item in self.source_classes)
            or any(not isinstance(item, StructuredFact) for item in self.facts)
            or any(not isinstance(item, QualityFlag) for item in self.quality_flags)
        ):
            raise ValidationError("invalid event collection member")
        if not self.title or self.title != self.title.strip():
            raise ValidationError("event title must be nonempty and trimmed")
        if (
            not self.evidence_observation_ids
            or tuple(sorted(set(self.evidence_observation_ids))) != self.evidence_observation_ids
        ):
            raise ValidationError("evidence observation IDs must be nonempty, sorted and unique")
        if any(not item.startswith("obs_") for item in self.evidence_observation_ids):
            raise ValidationError("invalid evidence observation ID")
        if not self.source_classes:
            raise ValidationError("source classes required")
        if tuple(sorted(fact.name for fact in self.facts)) != tuple(
            fact.name for fact in self.facts
        ):
            raise ValidationError("facts must be sorted by name")
        if len({fact.name for fact in self.facts}) != len(self.facts):
            raise ValidationError("duplicate fact name")
        for name in (
            "scheduled_for_ns",
            "announced_at_ns",
            "published_at_ns",
            "source_observed_at_ns",
            "ingested_at_ns",
            "effective_at_ns",
            "last_updated_at_ns",
        ):
            value = getattr(self, name)
            if value is not None:
                require_ns(value, name)
        for name in (
            "announced_at_ns",
            "published_at_ns",
            "source_observed_at_ns",
            "ingested_at_ns",
            "last_updated_at_ns",
        ):
            value = getattr(self, name)
            if value is not None and value > self.revision.available_from_ns:
                raise TemporalConsistencyError(f"{name} follows availability")
        for earlier, later in (
            (self.published_at_ns, self.source_observed_at_ns),
            (self.published_at_ns, self.ingested_at_ns),
            (self.source_observed_at_ns, self.ingested_at_ns),
        ):
            if earlier is not None and later is not None and earlier > later:
                raise TemporalConsistencyError("event source timestamps are out of order")
        if (
            self.revision.kind is RevisionKind.CANCELLATION
            and self.lifecycle is not LifecycleState.CANCELLED
        ):
            raise ValidationError("cancellation revision must be CANCELLED")
        if (
            self.revision.kind is RevisionKind.RESCHEDULE
            and self.lifecycle is not LifecycleState.RESCHEDULED
        ):
            raise ValidationError("reschedule revision must be RESCHEDULED")
        if (
            self.lifecycle is LifecycleState.CANCELLED
            and self.revision.kind is not RevisionKind.CANCELLATION
        ):
            raise ValidationError("CANCELLED requires cancellation revision")
        if (
            self.lifecycle is LifecycleState.RESCHEDULED
            and self.revision.kind is not RevisionKind.RESCHEDULE
        ):
            raise ValidationError("RESCHEDULED requires reschedule revision")

    @property
    def event_id(self) -> str:
        return self.identity.event_id

    @property
    def revision_id(self) -> str:
        return self.revision.revision_id(self.event_id)

    @property
    def event_payload_fingerprint(self) -> str:
        return fingerprint(
            "eie:event-payload:v1",
            (
                self.identity,
                self.lifecycle,
                self.confirmation,
                self.evidence_observation_ids,
                self.source_classes,
                self.title,
                self.scheduled_for_ns,
                self.announced_at_ns,
                self.published_at_ns,
                self.source_observed_at_ns,
                self.ingested_at_ns,
                self.effective_at_ns,
                self.last_updated_at_ns,
                self.facts,
                self.quality_flags,
            ),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:canonical-event:v1", self)
