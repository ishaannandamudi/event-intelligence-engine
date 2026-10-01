"""Deterministic append-only in-memory event history."""

from eie.core.errors import PersistenceError, TemporalConsistencyError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.events.models import CanonicalEvent
from eie.events.scope import ApplicabilityQuery, applies_directly
from eie.persistence.validation import validate_event_append
from eie.sources.models import RawSourceObservation


class InMemoryEventStore:
    def __init__(self) -> None:
        self._observations: dict[str, RawSourceObservation] = {}
        self._events: dict[str, list[CanonicalEvent]] = {}

    def append_observation(self, observation: RawSourceObservation) -> bool:
        previous = self._observations.get(observation.observation_id)
        if previous is not None:
            if previous != observation:
                raise PersistenceError(
                    "observation identity collision or inconsistent re-ingestion"
                )
            return False
        self._observations[observation.observation_id] = observation
        return True

    def get_observation(self, observation_id: str) -> RawSourceObservation | None:
        return self._observations.get(observation_id)

    def append_event(self, event: CanonicalEvent) -> bool:
        if not validate_event_append(self, event):
            return False
        self._events.setdefault(event.event_id, []).append(event)
        return True

    def get_event(self, event_id: str, as_of_ns: int) -> CanonicalEvent | None:
        require_ns(as_of_ns, "as_of_ns")
        history = self._events.get(event_id, ())
        return next(
            (event for event in reversed(history) if event.revision.available_from_ns <= as_of_ns),
            None,
        )

    def get_revisions(self, event_id: str) -> tuple[CanonicalEvent, ...]:
        return tuple(self._events.get(event_id, ()))

    def known_as_of(
        self,
        as_of_ns: int,
        query: ApplicabilityQuery | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        require_ns(as_of_ns, "as_of_ns")
        if start_ns is not None:
            require_ns(start_ns, "start_ns")
        if end_ns is not None:
            require_ns(end_ns, "end_ns")
        if start_ns is not None and end_ns is not None and end_ns < start_ns:
            raise TemporalConsistencyError("end_ns precedes start_ns")
        events = []
        for event_id in sorted(self._events):
            event = self.get_event(event_id, as_of_ns)
            if event is None or (
                query is not None and not applies_directly(event.identity.scope, query)
            ):
                continue
            event_time = (
                event.effective_at_ns
                if event.effective_at_ns is not None
                else event.scheduled_for_ns
            )
            if start_ns is not None and (event_time is None or event_time < start_ns):
                continue
            if end_ns is not None and (event_time is None or event_time >= end_ns):
                continue
            events.append(event)
        return tuple(events)

    def canonical_state_bytes(self) -> bytes:
        """Deterministic state artifact; no executable serialization."""
        return canonical_json_bytes(
            {
                "observations": [self._observations[key] for key in sorted(self._observations)],
                "events": [event for key in sorted(self._events) for event in self._events[key]],
            }
        )
