"""UTC integer nanoseconds since Unix epoch; datetimes have microsecond precision."""

from datetime import UTC, datetime, timedelta

from eie.core.errors import TemporalConsistencyError

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_NS = 2**63 - 1


def require_ns(value: int, name: str = "timestamp") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_NS:
        raise TemporalConsistencyError(f"{name} must be a nonnegative signed-64-bit UTC ns integer")
    return value


def datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalConsistencyError("timezone-aware datetime required")
    delta = value.astimezone(UTC) - EPOCH
    return require_ns(
        (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    )


def ns_to_datetime(value: int) -> datetime:
    require_ns(value)
    if value % 1000:
        raise TemporalConsistencyError("datetime cannot represent sub-microsecond nanoseconds")
    return EPOCH + timedelta(microseconds=value // 1000)
