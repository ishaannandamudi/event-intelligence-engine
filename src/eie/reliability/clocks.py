"""Clock precision is retained; only explicit UTC dates supply coarse intervals."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from eie.evidence.claims import TimePrecision, TimeValue


def exact(value: int | None) -> TimeValue | None:
    if value is None:
        return None
    day = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=value // 1_000_000_000)).date()
    return TimeValue(TimePrecision.EXACT, day.isoformat(), value)


def bounds(value: TimeValue | None, timezone: str | None = None) -> tuple[int, int] | None:
    if value is None or value.precision is TimePrecision.UNKNOWN:
        return None
    if value.timestamp_ns is not None:
        return value.timestamp_ns, value.timestamp_ns
    if timezone is None:
        return None
    assert value.date is not None
    day = datetime.fromisoformat(value.date).replace(tzinfo=ZoneInfo(timezone))

    def nanoseconds(stamp: datetime) -> int:
        delta = stamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000

    return nanoseconds(day), nanoseconds(day + timedelta(days=1)) - 1
