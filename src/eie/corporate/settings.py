"""Small, immutable corporate-ingestion and query limits."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import require_ns


@dataclass(frozen=True, slots=True)
class CorporateSettings:
    enabled: bool = False
    max_payload_bytes: int = 2_000_000
    max_items_per_batch: int = 1_000
    max_results: int = 10_000
    staleness_ns: int = 7 * 86_400_000_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidationError("corporate enabled must be boolean")
        for name in ("max_payload_bytes", "max_items_per_batch", "max_results"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"corporate {name} must be positive")
        require_ns(self.staleness_ns, "corporate staleness_ns")
        if self.staleness_ns == 0:
            raise ValidationError("corporate staleness threshold must be positive")
