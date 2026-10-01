"""Capabilities are explicit; optional operations live in separate protocols."""

from enum import StrEnum
from typing import Protocol

from eie.events.models import CanonicalEvent
from eie.sources.models import RawSourceObservation, SourceHealth, SourcePollResult


class SourceCapability(StrEnum):
    POLL = "POLL"
    FETCH = "FETCH"
    SCHEDULED = "SCHEDULED"
    NORMALIZE = "NORMALIZE"


class EventSource(Protocol):
    @property
    def source_name(self) -> str: ...

    @property
    def source_version(self) -> str: ...

    def health(self) -> SourceHealth: ...

    def capabilities(self) -> frozenset[SourceCapability]: ...


class PollingSource(EventSource, Protocol):
    def poll(self, as_of_ns: int) -> SourcePollResult: ...


class FetchingSource(EventSource, Protocol):
    def fetch(self, source_item_id: str, as_of_ns: int) -> RawSourceObservation | None: ...


class ScheduledSource(EventSource, Protocol):
    def scheduled_events(self, start_ns: int, end_ns: int, as_of_ns: int) -> SourcePollResult: ...


class EventNormalizer(Protocol):
    def normalize(self, observation: RawSourceObservation) -> tuple[CanonicalEvent, ...]: ...
