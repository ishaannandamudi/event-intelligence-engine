"""Precise clocks stay separate; dates describe intervals and never precise gaps."""

from datetime import UTC, datetime

from eie.core.errors import ValidationError
from eie.evidence.claims import TimePrecision, TimeValue
from eie.interactions.models import DAY, InteractionEvent, InteractionPolicy, InteractionWindow
from eie.interactions.schema import OrderingBasis
from eie.studies.models import SessionPolicy
from eie.studies.windows import offset_time


def exact(value: int | None) -> TimeValue:
    return (
        TimeValue(TimePrecision.UNKNOWN)
        if value is None
        else TimeValue(
            TimePrecision.EXACT,
            datetime.fromtimestamp(value // 1_000_000_000, UTC).date().isoformat(),
            value,
        )
    )


def time_value(row: InteractionEvent, basis: OrderingBasis) -> TimeValue:
    if basis is OrderingBasis.EXACT_EVENT_TIME:
        return row.occurrence
    if basis is OrderingBasis.SCHEDULED_TIME:
        return row.scheduled
    if basis is OrderingBasis.SOURCE_AVAILABILITY:
        return exact(row.source_available_ns)
    if basis is OrderingBasis.KNOWLEDGE_TIME:
        return exact(row.known_ns)
    if basis is OrderingBasis.COARSE_SESSION:
        value = row.occurrence
        return (
            value
            if value.precision is TimePrecision.DATE_PLUS_SESSION
            else TimeValue(TimePrecision.UNKNOWN)
        )
    return TimeValue(TimePrecision.UNKNOWN)


def interval(value: TimeValue, *, coarse: bool) -> tuple[int, int] | None:
    if value.timestamp_ns is not None:
        return value.timestamp_ns, value.timestamp_ns
    if not coarse or value.date is None:
        return None
    day = (
        datetime.fromisoformat(value.date).replace(tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    ).days
    return day * DAY, (day + 1) * DAY - 1


def window_interval(row: InteractionEvent, window: InteractionWindow) -> tuple[int, int] | None:
    bounds = interval(
        time_value(row, window.basis_for(row.event_type)), coarse=window.permit_coarse
    )
    if bounds is None:
        return None
    pre, post = window.durations(row.event_type)
    if window.session_policy is SessionPolicy.ELAPSED:
        return max(0, bounds[0] - pre), bounds[1] + post
    return (
        offset_time(bounds[0], -pre, window.session_policy, window.sessions),
        offset_time(bounds[1], post, window.session_policy, window.sessions),
    )


def horizon_end(root: InteractionEvent, policy: InteractionPolicy) -> int | None:
    t = time_value(root, policy.chronology_basis).timestamp_ns
    if t is None:
        return None
    if policy.horizon_sessions is None:
        return t + policy.horizon_ns
    sessions = [
        s for s in policy.window.sessions if s.available_ns <= root.known_ns and s.close_ns > t
    ]
    if len(sessions) < policy.horizon_sessions:
        raise ValidationError("supplied calendar does not cover propagation session horizon")
    return sessions[policy.horizon_sessions - 1].close_ns
