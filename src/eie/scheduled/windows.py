"""Explicit policy windows, separate from event lifecycle."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import MAX_NS, require_ns
from eie.events.lifecycle import EventWindowState
from eie.events.taxonomy import EventFamily, EventType


@dataclass(frozen=True, slots=True)
class EventWindowPolicy:
    pre_event_ns: int = 0
    event_window_ns: int = 0
    post_event_ns: int = 0

    def __post_init__(self) -> None:
        for name in ("pre_event_ns", "event_window_ns", "post_event_ns"):
            require_ns(getattr(self, name), name)
        if self.event_window_ns + self.post_event_ns > MAX_NS:
            raise ValidationError("combined event and post windows overflow UTC ns")


@dataclass(frozen=True, slots=True)
class WindowPolicySet:
    default: EventWindowPolicy = EventWindowPolicy()
    family_windows: tuple[tuple[EventFamily, EventWindowPolicy], ...] = ()
    event_type_windows: tuple[tuple[EventType, EventWindowPolicy], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.default, EventWindowPolicy):
            raise ValidationError("typed default window policy required")
        if not isinstance(self.family_windows, tuple) or not isinstance(
            self.event_type_windows, tuple
        ):
            raise ValidationError("window overrides must be immutable tuples")
        if any(
            not isinstance(family, EventFamily) or not isinstance(policy, EventWindowPolicy)
            for family, policy in self.family_windows
        ):
            raise ValidationError("invalid family window override")
        if any(
            not isinstance(event_type, EventType) or not isinstance(policy, EventWindowPolicy)
            for event_type, policy in self.event_type_windows
        ):
            raise ValidationError("invalid event type window override")
        families = tuple(family.value for family, _ in self.family_windows)
        types = tuple(event_type.code for event_type, _ in self.event_type_windows)
        if families != tuple(sorted(set(families))) or types != tuple(sorted(set(types))):
            raise ValidationError("window override keys must be sorted and unique")

    def policy_for(self, event_type: EventType) -> EventWindowPolicy:
        for key, policy in self.event_type_windows:
            if key == event_type:
                return policy
        for family, policy in self.family_windows:
            if family is event_type.family:
                return policy
        return self.default

    @property
    def max_pre_ns(self) -> int:
        return max(policy.pre_event_ns for policy in self._all())

    @property
    def max_after_ns(self) -> int:
        return max(policy.event_window_ns + policy.post_event_ns for policy in self._all())

    def _all(self) -> tuple[EventWindowPolicy, ...]:
        return (
            (self.default,)
            + tuple(policy for _, policy in self.family_windows)
            + tuple(policy for _, policy in self.event_type_windows)
        )


@dataclass(frozen=True, slots=True)
class ScheduledSettings:
    windows: WindowPolicySet = WindowPolicySet()
    max_query_horizon_ns: int = 5 * 366 * 86_400_000_000_000
    max_results: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.windows, WindowPolicySet):
            raise ValidationError("typed scheduled window settings required")
        require_ns(self.max_query_horizon_ns, "max_query_horizon_ns")
        if self.max_query_horizon_ns == 0:
            raise ValidationError("maximum query horizon must be positive")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or self.max_results < 1
        ):
            raise ValidationError("max_results must be a positive integer")


def window_state(as_of_ns: int, event_time_ns: int, policy: EventWindowPolicy) -> EventWindowState:
    require_ns(as_of_ns, "as_of_ns")
    require_ns(event_time_ns, "event_time_ns")
    if not isinstance(policy, EventWindowPolicy):
        raise ValidationError("typed event window policy required")
    event_end = require_ns(event_time_ns + policy.event_window_ns, "event_window_end_ns")
    post_end = require_ns(event_end + policy.post_event_ns, "post_event_end_ns")
    if event_time_ns - policy.pre_event_ns <= as_of_ns < event_time_ns:
        return EventWindowState.PRE_EVENT
    if event_time_ns <= as_of_ns < event_end:
        return EventWindowState.EVENT_WINDOW
    if event_end <= as_of_ns < post_end:
        return EventWindowState.POST_EVENT
    return EventWindowState.NORMAL
