"""Replay query contract and tiny store-backed implementation."""

from typing import Protocol

from eie.events.scope import ApplicabilityQuery
from eie.persistence.protocols import EventStore
from eie.replay.clock import ReplayClock
from eie.replay.models import KnownEvents


class ReplayReader(Protocol):
    def events_known_at(
        self, as_of_ns: int, query: ApplicabilityQuery | None = None
    ) -> KnownEvents: ...


class StoreReplayReader:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def events_known_at(
        self, as_of_ns: int, query: ApplicabilityQuery | None = None
    ) -> KnownEvents:
        return KnownEvents(as_of_ns, self.store.known_as_of(as_of_ns, query))

    def at_clock(self, clock: ReplayClock, query: ApplicabilityQuery | None = None) -> KnownEvents:
        return self.events_known_at(clock.as_of_ns, query)
