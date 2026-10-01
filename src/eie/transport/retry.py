"""Finite retries for transport failures and selected transient HTTP statuses."""

from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from threading import Event

from eie.core.errors import ValidationError
from eie.runtime.clock import RuntimeClock
from eie.runtime.config import positive_int, positive_number
from eie.transport.errors import AcquisitionCancelled, TransientTransportError
from eie.transport.models import HttpPolicy, HttpResponse
from eie.transport.protocols import HttpTransport
from eie.transport.rate_limit import RateLimiter

TRANSIENT_STATUSES = frozenset((429, 500, 502, 503, 504))


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0

    def __post_init__(self) -> None:
        positive_int(self.max_attempts, 10)
        positive_number(self.base_delay, 120)
        positive_number(self.max_delay, 120)
        if self.base_delay > self.max_delay:
            raise ValidationError("retry base exceeds maximum delay")


def retry_after(value: str | None, now_ns: int, maximum: float) -> float | None:
    if value is None:
        return None
    if value.isascii() and value.isdecimal():
        # Bound integer parsing before conversion (including malicious huge digits).
        return maximum if len(value) > 8 else min(float(value), maximum)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return min(max(0.0, date.timestamp() - now_ns / 1_000_000_000), maximum)
    except (ValueError, TypeError, OverflowError):
        return None


class RetryingClient:
    def __init__(
        self,
        transport: HttpTransport,
        policy: HttpPolicy,
        retry: RetryPolicy,
        limiter: RateLimiter,
        clock: RuntimeClock,
    ) -> None:
        if policy.max_redirects:
            raise ValidationError("rate-limited clients require redirects disabled")
        self.transport, self.policy, self.retry = transport, policy, retry
        self.limiter, self.clock = limiter, clock

    def fetch(
        self, url: str, stop: Event, conditionals: tuple[tuple[str, str], ...] = ()
    ) -> HttpResponse:
        self.policy.validate_url(url)
        for attempt in range(1, self.retry.max_attempts + 1):
            self.limiter.acquire(stop)
            delay = min(self.retry.max_delay, self.retry.base_delay * 2 ** (attempt - 1))
            try:
                response = self.transport.fetch(url, self.policy, conditionals)
            except TransientTransportError:
                if attempt == self.retry.max_attempts:
                    raise
            else:
                if response.status not in TRANSIENT_STATUSES:
                    return replace(response, attempts=attempt)
                requested = retry_after(
                    response.header("retry-after"), self.clock.utc_ns(), self.retry.max_delay
                )
                delay = max(delay, requested or 0)
                if response.status == 429:
                    self.limiter.observe_429(delay)
                if attempt == self.retry.max_attempts:
                    return replace(response, attempts=attempt)
            if self.clock.wait(delay, stop):
                raise AcquisitionCancelled("acquisition stopped during retry wait")
        raise AcquisitionCancelled("retry policy exhausted")
