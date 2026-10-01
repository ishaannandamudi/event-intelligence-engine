"""Event store behavior required by causal replay."""

from typing import Protocol

from eie.events.models import CanonicalEvent
from eie.events.scope import ApplicabilityQuery
from eie.sources.models import RawSourceObservation


class EventStore(Protocol):
    def append_observation(self, observation: RawSourceObservation) -> bool: ...

    def get_observation(self, observation_id: str) -> RawSourceObservation | None: ...

    def append_event(self, event: CanonicalEvent) -> bool: ...

    def get_event(self, event_id: str, as_of_ns: int) -> CanonicalEvent | None: ...

    def get_revisions(self, event_id: str) -> tuple[CanonicalEvent, ...]: ...

    def known_as_of(
        self,
        as_of_ns: int,
        query: ApplicabilityQuery | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> tuple[CanonicalEvent, ...]: ...

    def canonical_state_bytes(self) -> bytes: ...
