"""Earnings schedule, release, and call payloads."""

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.corporate.types import EarningsPeriod, ReleaseTiming, SchedulePrecision, _date
from eie.sources.models import SECRET_KEYS


@dataclass(frozen=True, slots=True)
class EarningsSchedule:
    period: EarningsPeriod
    scheduled_date: str | None
    scheduled_for_ns: int | None
    release_timing: ReleaseTiming
    precision: SchedulePrecision

    def __post_init__(self) -> None:
        if (
            not isinstance(self.period, EarningsPeriod)
            or not isinstance(self.release_timing, ReleaseTiming)
            or not isinstance(self.precision, SchedulePrecision)
        ):
            raise ValidationError("typed earnings schedule required")
        _date(self.scheduled_date, "scheduled_date")
        if self.scheduled_for_ns is not None:
            require_ns(self.scheduled_for_ns, "scheduled_for_ns")
        if self.precision is SchedulePrecision.EXACT_TIMESTAMP:
            if self.scheduled_for_ns is None or self.scheduled_date is None:
                raise ValidationError("exact schedule requires date and time")
        elif self.scheduled_for_ns is not None:
            raise ValidationError("coarse schedule cannot carry fabricated exact time")
        if (
            self.precision in (SchedulePrecision.DATE_ONLY, SchedulePrecision.DATE_PLUS_SESSION)
            and self.scheduled_date is None
        ):
            raise ValidationError("dated schedule requires date")
        if (
            self.precision is SchedulePrecision.DATE_PLUS_SESSION
            and self.release_timing is ReleaseTiming.UNKNOWN
        ):
            raise ValidationError("session precision requires release timing")
        if (
            self.precision is SchedulePrecision.DATE_ONLY
            and self.release_timing is not ReleaseTiming.UNKNOWN
        ):
            raise ValidationError("date-only precision cannot imply session timing")
        if self.precision is SchedulePrecision.UNKNOWN and self.scheduled_date is not None:
            raise ValidationError("unknown precision cannot carry date")
        if (
            self.precision is SchedulePrecision.UNKNOWN
            and self.release_timing is not ReleaseTiming.UNKNOWN
        ):
            raise ValidationError("unknown schedule cannot imply session timing")


@dataclass(frozen=True, slots=True)
class EarningsRelease:
    period: EarningsPeriod
    published_at_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.period, EarningsPeriod):
            raise ValidationError("typed earnings period required")
        require_ns(self.published_at_ns, "published_at_ns")


@dataclass(frozen=True, slots=True)
class EarningsCall:
    period: EarningsPeriod
    scheduled_for_ns: int
    webcast_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.period, EarningsPeriod):
            raise ValidationError("typed earnings period required")
        require_ns(self.scheduled_for_ns, "call scheduled_for_ns")
        if self.webcast_url is not None:
            try:
                parts = urlsplit(self.webcast_url)
                invalid = (
                    parts.scheme != "https"
                    or not parts.hostname
                    or parts.username
                    or parts.password
                    or parts.fragment
                    or any(key.lower() in SECRET_KEYS for key, _ in parse_qsl(parts.query))
                )
            except ValueError as exc:
                raise ValidationError("webcast URL must be credential-free HTTPS") from exc
            if invalid:
                raise ValidationError("webcast URL must be credential-free HTTPS")
