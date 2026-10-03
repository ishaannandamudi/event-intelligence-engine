"""Bounded Python APIs; no market response enters membership or ordering."""

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.interactions.collection import dedupe
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import (
    EventCollision,
    EventSequence,
    EventStateTransition,
    InteractionContext,
    InteractionQuery,
    PropagationChronology,
)
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import OrderingBasis
from eie.interactions.sequences import sequence
from eie.interactions.transitions import transitions


class InteractionQueries:
    def __init__(self, repository: InteractionRepository) -> None:
        self.repository = repository

    def interaction_context(self, query: InteractionQuery) -> InteractionContext:
        return InteractionEngine(self.repository).interaction_context(query)

    def collisions(self, query: InteractionQuery) -> tuple[EventCollision, ...]:
        repo = self.repository
        return tuple(
            repo.artifact(i, EventCollision) for i in self.interaction_context(query).collision_ids
        )

    def propagation(self, query: InteractionQuery) -> tuple[PropagationChronology, ...]:
        repo = self.repository
        return tuple(
            repo.artifact(i, PropagationChronology)
            for i in self.interaction_context(query).chronology_ids
        )

    def sequence(
        self,
        dataset: str,
        entity: str,
        start: int,
        end: int,
        known: int,
        policy_id: str,
        basis: OrderingBasis = OrderingBasis.EXACT_EVENT_TIME,
        event_types: tuple[str, ...] = (),
        event_resolution_id: str | None = None,
    ) -> EventSequence:
        for t in (start, end, known):
            require_ns(t)
        if end < start or not isinstance(basis, OrderingBasis):
            raise ValidationError("invalid sequence range/basis")
        repo = self.repository
        with repo.store.transaction():
            policy = repo.policy(policy_id)
            rows = repo.search(
                dataset, known, basis, start, end, policy.max_sequence_events, entities=(entity,)
            )
            if event_types:
                rows = tuple(r for r in rows if r.event_type.code in event_types)
            if event_resolution_id is not None:
                run = repo.evidence.stored_event_run(event_resolution_id)
                if (
                    run.dataset_id != dataset
                    or run.known_as_of_ns > known
                    or run.truncated_event_ids
                ):
                    raise ValidationError("sequence alias run must be visible and untruncated")
                rows = dedupe(rows, tuple(g.member_event_ids for g in run.groups))
            value = sequence(rows, dataset, entity, start, end, known, basis, event_resolution_id)
            from eie.persistence import codec

            if len(codec.dumps(value)) > policy.max_output_bytes:
                raise ValidationError("sequence serialized output bound exceeded")
            repo.put(value)
            return value

    def state_transitions(
        self, dataset: str, thread: str, known: int, policy_id: str
    ) -> tuple[EventStateTransition, ...]:
        require_ns(known)
        repo = self.repository
        with repo.store.transaction():
            policy = repo.policy(policy_id)
            rows = repo.search(
                dataset,
                known,
                OrderingBasis.KNOWLEDGE_TIME,
                0,
                known,
                policy.max_sequence_events,
                thread=thread,
                history=True,
            )
            values = transitions(rows)
            from eie.persistence import codec

            if sum(len(codec.dumps(v)) for v in values) > policy.max_output_bytes:
                raise ValidationError("transition serialized output bound exceeded")
            for value in values:
                repo.put(value)
            return values
