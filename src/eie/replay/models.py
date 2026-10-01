"""Minimal EIE-0 replay result; full intelligence snapshot is deferred."""

from dataclasses import dataclass

from eie.core.time import require_ns
from eie.events.models import CanonicalEvent


@dataclass(frozen=True, slots=True)
class KnownEvents:
    as_of_ns: int
    events: tuple[CanonicalEvent, ...]

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if any(event.revision.available_from_ns > self.as_of_ns for event in self.events):
            raise ValueError("future event in replay result")
