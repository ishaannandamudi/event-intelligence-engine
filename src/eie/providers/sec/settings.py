"""SEC EDGAR ingestion policy; disabled and offline by default."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import require_ns


@dataclass(frozen=True, slots=True)
class SecSettings:
    enabled: bool = False
    allow_issuer_bootstrap: bool = True
    max_payload_bytes: int = 2_000_000
    max_filings_per_ingest: int = 1_000
    staleness_ns: int = 7 * 86_400_000_000_000
    timeout_seconds: int = 10
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.allow_issuer_bootstrap, bool):
            raise ValidationError("SEC feature flags must be boolean")
        for name in ("max_payload_bytes", "max_filings_per_ingest", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"SEC {name} must be positive")
        if self.timeout_seconds > 120:
            raise ValidationError("SEC timeout must be at most 120 seconds")
        require_ns(self.staleness_ns, "SEC staleness_ns")
        if self.staleness_ns == 0:
            raise ValidationError("SEC staleness threshold must be positive")
        if self.user_agent is not None and (
            not isinstance(self.user_agent, str)
            or not self.user_agent
            or self.user_agent != self.user_agent.strip()
            or "\n" in self.user_agent
            or "\r" in self.user_agent
        ):
            raise ValidationError("SEC user agent must be safe trimmed text")
