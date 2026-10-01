"""Caller-controlled replay clock with no wall-clock dependency."""

from dataclasses import dataclass

from eie.core.errors import ReplayError
from eie.core.time import require_ns


@dataclass(slots=True)
class ReplayClock:
    as_of_ns: int

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")

    def set(self, as_of_ns: int) -> None:
        self.as_of_ns = require_ns(as_of_ns, "as_of_ns")

    def advance(self, delta_ns: int) -> None:
        if isinstance(delta_ns, bool) or not isinstance(delta_ns, int) or delta_ns < 0:
            raise ReplayError("advance requires nonnegative integer nanoseconds")
        self.as_of_ns = require_ns(self.as_of_ns + delta_ns, "as_of_ns")
