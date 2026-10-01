"""Semantic event identity excludes mutable schedule and revision data."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.scope import EventScope
from eie.events.taxonomy import EventType


@dataclass(frozen=True, slots=True)
class EventIdentity:
    event_type: EventType
    scope: EventScope
    semantic_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EventType) or not isinstance(self.scope, EventScope):
            raise ValidationError("typed event type and scope required")
        if not self.semantic_key or self.semantic_key != self.semantic_key.strip():
            raise ValidationError("stable semantic key required")

    @property
    def event_id(self) -> str:
        return "evt_" + fingerprint("eie:event-identity:v1", self)
