"""Result-relevant consensus and prior-history policies."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.expectations.units import number


class CutoffPolicy(StrEnum):
    STRICT_BEFORE = "STRICT_BEFORE"
    AT_OR_BEFORE = "AT_OR_BEFORE"


class ScaleMethod(StrEnum):
    MAD = "MAD"
    STANDARD_DEVIATION = "STANDARD_DEVIATION"


class ProviderPooling(StrEnum):
    SAME_PROVIDER = "SAME_PROVIDER"
    EXPLICIT_POOL = "EXPLICIT_POOL"


@dataclass(frozen=True, slots=True)
class ExpectationsSettings:
    enabled: bool = False
    minimum_history: int = 5
    normalization_method: ScaleMethod = ScaleMethod.MAD
    history_window_events: int | None = None
    staleness_ns: int = 7 * 86_400_000_000_000
    consensus_cutoff_policy: CutoffPolicy = CutoffPolicy.STRICT_BEFORE
    provider_pooling_policy: ProviderPooling = ProviderPooling.SAME_PROVIDER
    allow_relative_surprise: bool = False
    relative_min_abs_consensus: float = 0.01
    max_payload_bytes: int = 2_000_000
    max_items_per_batch: int = 1000

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.allow_relative_surprise, bool):
            raise ValidationError("expectations flags must be boolean")
        for name in ("minimum_history", "max_payload_bytes", "max_items_per_batch"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (3 if name == "minimum_history" else 1)
            ):
                raise ValidationError(f"invalid expectations {name}")
        if self.history_window_events is not None and (
            isinstance(self.history_window_events, bool)
            or not isinstance(self.history_window_events, int)
            or self.history_window_events < self.minimum_history
        ):
            raise ValidationError("history window must accommodate minimum history")
        if (
            not isinstance(self.normalization_method, ScaleMethod)
            or not isinstance(self.consensus_cutoff_policy, CutoffPolicy)
            or not isinstance(self.provider_pooling_policy, ProviderPooling)
        ):
            raise ValidationError("typed expectations policies required")
        require_ns(self.staleness_ns)
        number(self.relative_min_abs_consensus)
        if self.relative_min_abs_consensus <= 0:
            raise ValidationError("relative surprise threshold must be positive")
