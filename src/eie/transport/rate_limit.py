"""Per-provider sliding request windows, with explicit 429 cooldown."""

from collections import deque
from dataclasses import dataclass
from threading import Event, Lock

from eie.runtime.clock import RuntimeClock
from eie.runtime.config import positive_int, positive_number
from eie.transport.errors import AcquisitionCancelled


@dataclass(frozen=True, slots=True)
class RatePolicy:
    capacity: int = 2
    window_seconds: float = 1.0

    def __post_init__(self) -> None:
        positive_int(self.capacity, 1000)
        positive_number(self.window_seconds, 3600)


@dataclass(frozen=True, slots=True)
class RateState:
    capacity: int
    throttled: bool
    next_eligible_monotonic_ns: int
    observed_429: int


class RateLimiter:
    def __init__(self, policy: RatePolicy, clock: RuntimeClock) -> None:
        self.policy, self.clock = policy, clock
        self._requests: deque[int] = deque()
        self._pause_until = 0
        self._observed_429 = 0
        self._lock = Lock()

    def _eligible(self, now: int) -> int:
        window = int(self.policy.window_seconds * 1_000_000_000)
        while self._requests and self._requests[0] + window <= now:
            self._requests.popleft()
        return max(
            now,
            self._pause_until,
            self._requests[0] + window if len(self._requests) >= self.policy.capacity else now,
        )

    def acquire(self, stop: Event) -> None:
        while not stop.is_set():
            with self._lock:
                now = self.clock.monotonic_ns()
                eligible = self._eligible(now)
                if eligible == now:
                    self._requests.append(now)
                    return
            if self.clock.wait((eligible - now) / 1_000_000_000, stop):
                break
        raise AcquisitionCancelled("acquisition stopped during rate wait")

    def observe_429(self, delay: float) -> None:
        with self._lock:
            self._observed_429 += 1
            self._pause_until = max(
                self._pause_until, self.clock.monotonic_ns() + int(delay * 1_000_000_000)
            )

    def state(self) -> RateState:
        with self._lock:
            now = self.clock.monotonic_ns()
            eligible = self._eligible(now)
            return RateState(self.policy.capacity, eligible > now, eligible, self._observed_429)
