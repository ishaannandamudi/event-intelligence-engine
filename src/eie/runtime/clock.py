"""Wall UTC for acquisition, monotonic durations for operational scheduling."""

from threading import Event, Lock
from time import monotonic_ns, time_ns
from typing import Protocol

from eie.core.errors import ValidationError
from eie.core.time import require_ns


class RuntimeClock(Protocol):
    def utc_ns(self) -> int: ...
    def monotonic_ns(self) -> int: ...
    def wait(self, seconds: float, stop: Event) -> bool: ...


class SystemClock:
    def utc_ns(self) -> int:
        return time_ns()

    def monotonic_ns(self) -> int:
        return monotonic_ns()

    def wait(self, seconds: float, stop: Event) -> bool:
        return stop.wait(seconds)


class ManualClock:
    def __init__(self, utc_ns: int = 1, monotonic_ns: int = 0) -> None:
        self._utc = require_ns(utc_ns)
        self._monotonic = require_ns(monotonic_ns)
        self._lock = Lock()

    def utc_ns(self) -> int:
        with self._lock:
            return self._utc

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._monotonic

    def advance(self, nanoseconds: int) -> None:
        require_ns(nanoseconds)
        with self._lock:
            self._utc = require_ns(self._utc + nanoseconds)
            self._monotonic = require_ns(self._monotonic + nanoseconds)

    def wait(self, seconds: float, stop: Event) -> bool:
        if seconds < 0:
            raise ValidationError("negative runtime wait")
        if not stop.is_set():
            self.advance(int(seconds * 1_000_000_000))
        return stop.is_set()
