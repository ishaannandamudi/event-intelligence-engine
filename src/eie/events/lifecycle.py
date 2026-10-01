"""Validated event lifecycle; window state is an independent contract."""

from enum import StrEnum

from eie.core.errors import InvalidLifecycleTransition


class LifecycleState(StrEnum):
    SCHEDULED = "SCHEDULED"
    CONFIRMED = "CONFIRMED"
    IMMINENT = "IMMINENT"
    RELEASED = "RELEASED"
    UPDATED = "UPDATED"
    POST_EVENT = "POST_EVENT"
    NORMALIZED = "NORMALIZED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"


class EventWindowState(StrEnum):
    PRE_EVENT = "PRE_EVENT"
    EVENT_WINDOW = "EVENT_WINDOW"
    POST_EVENT = "POST_EVENT"
    NORMAL = "NORMAL"


TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.SCHEDULED: frozenset(
        {
            LifecycleState.CONFIRMED,
            LifecycleState.IMMINENT,
            LifecycleState.RELEASED,
            LifecycleState.CANCELLED,
            LifecycleState.RESCHEDULED,
        }
    ),
    LifecycleState.CONFIRMED: frozenset(
        {
            LifecycleState.IMMINENT,
            LifecycleState.RELEASED,
            LifecycleState.CANCELLED,
            LifecycleState.RESCHEDULED,
        }
    ),
    LifecycleState.IMMINENT: frozenset(
        {LifecycleState.RELEASED, LifecycleState.CANCELLED, LifecycleState.RESCHEDULED}
    ),
    LifecycleState.RESCHEDULED: frozenset(
        {LifecycleState.SCHEDULED, LifecycleState.CONFIRMED, LifecycleState.CANCELLED}
    ),
    LifecycleState.RELEASED: frozenset(
        {LifecycleState.UPDATED, LifecycleState.POST_EVENT, LifecycleState.NORMALIZED}
    ),
    LifecycleState.UPDATED: frozenset(
        {LifecycleState.UPDATED, LifecycleState.POST_EVENT, LifecycleState.NORMALIZED}
    ),
    LifecycleState.POST_EVENT: frozenset({LifecycleState.UPDATED, LifecycleState.NORMALIZED}),
    LifecycleState.NORMALIZED: frozenset({LifecycleState.UPDATED}),
    LifecycleState.CANCELLED: frozenset(),
}


def validate_transition(previous: LifecycleState, current: LifecycleState) -> None:
    if current not in TRANSITIONS[previous]:
        raise InvalidLifecycleTransition(f"{previous} -> {current} is forbidden")
