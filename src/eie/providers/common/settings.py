"""Immutable official-source policy; no provider is active by default."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import require_ns

PROVIDER_IDS = ("BEA", "BLS", "FEDERAL_RESERVE")


@dataclass(frozen=True, slots=True)
class OfficialProviderSettings:
    enabled_ids: tuple[str, ...] = ()
    staleness_ns: int = 7 * 86_400_000_000_000
    max_payload_bytes: int = 1_000_000
    timeout_seconds: int = 10
    user_agent: str = "EIE/0.3 official-source adapter"

    def __post_init__(self) -> None:
        if self.enabled_ids != tuple(sorted(set(self.enabled_ids))) or any(
            name not in PROVIDER_IDS for name in self.enabled_ids
        ):
            raise ValidationError("enabled provider IDs must be sorted and known")
        require_ns(self.staleness_ns, "staleness_ns")
        if self.staleness_ns == 0:
            raise ValidationError("provider staleness threshold must be positive")
        if (
            isinstance(self.max_payload_bytes, bool)
            or not isinstance(self.max_payload_bytes, int)
            or self.max_payload_bytes < 1
        ):
            raise ValidationError("maximum provider payload must be positive")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ValidationError("provider timeout must be 1 through 120 seconds")
        if (
            not isinstance(self.user_agent, str)
            or not self.user_agent
            or self.user_agent != self.user_agent.strip()
            or "\n" in self.user_agent
            or "\r" in self.user_agent
        ):
            raise ValidationError("user agent must be a safe nonempty string")
