"""Immutable operational settings, excluded from domain configuration fingerprints."""

import math
from dataclasses import dataclass

from eie.core.errors import ValidationError

RUNTIME_SCHEMA_VERSION = "1.0"


def positive_number(value: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValidationError("finite positive bounded duration required")


def positive_int(value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError("positive bounded integer required")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    enabled: bool = False
    worker_count: int = 1
    fetch_count: int = 2
    queue_capacity: int = 16
    shutdown_timeout: float = 30.0
    scheduler_resolution: float = 0.1

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidationError("typed runtime enabled flag required")
        positive_int(self.worker_count, 32)
        positive_int(self.fetch_count, 32)
        positive_int(self.queue_capacity, 10_000)
        positive_number(self.shutdown_timeout, 300)
        positive_number(self.scheduler_resolution, 60)
