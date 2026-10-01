"""Historical schedule index over the append-only canonical event store."""

from bisect import bisect_left, bisect_right, insort

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, TemporalConsistencyError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.events.scope import ApplicabilityQuery
from eie.persistence.memory import InMemoryEventStore
from eie.persistence.protocols import EventStore
from eie.scheduled.coverage import CoverageLedger, CoverageRecord
from eie.scheduled.models import CoverageSummary, ScheduleRevision
from eie.sources.models import RawSourceObservation


class ScheduledEventIndex:
    """Owns schedule insertion; direct writes to its store bypass this index."""

    def __init__(self, settings: Settings, store: EventStore | None = None) -> None:
        self.settings = settings
        self.store: EventStore = store if store is not None else InMemoryEventStore()
        self.coverage = CoverageLedger()
        self._revisions: dict[str, list[ScheduleRevision]] = {}
        self._times: list[tuple[int, str, int]] = []
        self._first_known: list[int] = []

    def append_observation(self, observation: RawSourceObservation) -> bool:
        if observation.source.source_class not in self.settings.accepted_source_classes:
            raise PersistenceError("source class not accepted by configuration")
        return self.store.append_observation(observation)

    def append_coverage(self, record: CoverageRecord) -> bool:
        return self.coverage.append(record)

    def append(self, revision: ScheduleRevision) -> bool:
        event = revision.event
        history = self._revisions.get(event.event_id, [])
        matching = next(
            (item for item in history if item.event.revision.number == event.revision.number),
            None,
        )
        if matching is not None:
            if matching != revision:
                raise PersistenceError("schedule revision identity collision")
            return False
        previous_number = history[-1].event.revision.number if history else 0
        if history and event.revision.number != previous_number + 1:
            raise PersistenceError("schedule revision gap")
        if not history and event.revision.number > 1:
            stored = self.store.get_revisions(event.event_id)
            if len(stored) != event.revision.number - 1 or stored[-1].scheduled_for_ns is not None:
                raise PersistenceError("first exact schedule must follow date-only history")
        elif not history and event.revision.number != 1:
            raise PersistenceError("schedule revision gap")
        for claim in revision.claims:
            observation = self.store.get_observation(claim.observation_id)
            if observation is None:
                raise PersistenceError("schedule claim lacks raw observation")
            if observation.source.source_class != claim.source_class:
                raise PersistenceError("schedule claim source class disagrees with evidence")
            if observation.ingested_at_ns > claim.available_from_ns:
                raise TemporalConsistencyError("schedule claim predates raw observation")
        order = self.settings.source_precedence.order_for("scheduled_for")
        rank = {source_class: position for position, source_class in enumerate(order)}
        best_rank = min(rank[claim.source_class] for claim in revision.claims)
        top = tuple(claim for claim in revision.claims if rank[claim.source_class] == best_rank)
        if revision.selected_observation_id not in {claim.observation_id for claim in top}:
            raise PersistenceError("selected schedule claim violates source precedence")
        if len({claim.proposed_time_ns for claim in top}) > 1:
            if not revision.adjudication_reason_codes:
                raise PersistenceError(
                    "equal-precedence schedule conflict needs adjudication reason"
                )
        elif revision.selected_observation_id != min(claim.observation_id for claim in top):
            raise PersistenceError(
                "same-priority equivalent claims require deterministic selection"
            )
        if not self.store.append_event(event):
            raise PersistenceError("canonical revision exists outside scheduled index")
        self._revisions.setdefault(event.event_id, []).append(revision)
        assert event.scheduled_for_ns is not None
        insort(self._times, (event.scheduled_for_ns, event.event_id, event.revision.number))
        if not history:
            insort(self._first_known, event.revision.available_from_ns)
        return True

    def history(self, event_id: str) -> tuple[ScheduleRevision, ...]:
        return tuple(self._revisions.get(event_id, ()))

    def resolve(self, event_id: str, as_of_ns: int) -> ScheduleRevision | None:
        require_ns(as_of_ns, "as_of_ns")
        return next(
            (
                revision
                for revision in reversed(self._revisions.get(event_id, ()))
                if revision.event.revision.available_from_ns <= as_of_ns
            ),
            None,
        )

    def candidate_ids(self, start_ns: int, end_ns: int) -> tuple[str, ...]:
        require_ns(start_ns, "start_ns")
        require_ns(end_ns, "end_ns")
        if end_ns < start_ns:
            raise TemporalConsistencyError("event-time range is reversed")
        first = bisect_left(self._times, (start_ns, "", 0))
        last = bisect_left(self._times, (end_ns, "", 0))
        return tuple(sorted({event_id for _, event_id, _ in self._times[first:last]}))

    def known_ids(self, as_of_ns: int) -> tuple[str, ...]:
        require_ns(as_of_ns, "as_of_ns")
        return tuple(
            event_id
            for event_id in sorted(self._revisions)
            if self._revisions[event_id][0].event.revision.available_from_ns <= as_of_ns
        )

    def coverage_summary(
        self, as_of_ns: int, start_ns: int, end_ns: int, query: ApplicabilityQuery | None
    ) -> CoverageSummary:
        has_known = bisect_right(self._first_known, as_of_ns) > 0
        return self.coverage.summary(as_of_ns, start_ns, end_ns, query, has_known)

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "store": self.store.canonical_state_bytes().decode("utf-8"),
                "schedules": [
                    revision
                    for event_id in sorted(self._revisions)
                    for revision in self._revisions[event_id]
                ],
                "coverage": self.coverage.records(),
            }
        )
