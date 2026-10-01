"""Shared causal insertion checks for memory and durable event stores."""

from eie.core.errors import PersistenceError, TemporalConsistencyError
from eie.events.lifecycle import validate_transition
from eie.events.models import CanonicalEvent
from eie.events.revisions import RevisionKind
from eie.persistence.protocols import EventStore


def validate_event_append(store: EventStore, event: CanonicalEvent) -> bool:
    superseded_id = event.revision.supersedes_event_id
    if superseded_id is not None and (
        superseded_id == event.event_id
        or store.get_event(superseded_id, event.revision.available_from_ns) is None
    ):
        raise PersistenceError("superseded event must be a different known event")
    history = store.get_revisions(event.event_id)
    if event.revision.number <= len(history):
        if history[event.revision.number - 1] != event:
            raise PersistenceError("revision identity collision")
        return False
    if event.revision.number != len(history) + 1:
        raise PersistenceError("revision gap")
    if history:
        previous = history[-1]
        if event.identity != previous.identity:
            raise PersistenceError("semantic identity changed")
        if event.revision.available_from_ns <= previous.revision.available_from_ns:
            raise TemporalConsistencyError("revision availability must increase")
        validate_transition(previous.lifecycle, event.lifecycle)
        if event.revision.kind is RevisionKind.RESCHEDULE and (
            previous.scheduled_for_ns is None
            or event.scheduled_for_ns is None
            or previous.scheduled_for_ns == event.scheduled_for_ns
        ):
            raise PersistenceError("reschedule requires changed existing schedule")
    elif event.revision.kind is not RevisionKind.ORIGINAL:
        raise PersistenceError("first revision must be original")
    classes = set()
    for identity in event.evidence_observation_ids:
        observation = store.get_observation(identity)
        if observation is None:
            raise PersistenceError("missing raw evidence")
        if observation.ingested_at_ns > event.revision.available_from_ns:
            raise TemporalConsistencyError("canonical event predates raw evidence")
        classes.add(observation.source.source_class)
    if classes != set(event.source_classes):
        raise PersistenceError("event source classes disagree with evidence")
    return True
