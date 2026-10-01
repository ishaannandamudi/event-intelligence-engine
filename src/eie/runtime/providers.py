"""Deterministic registration and concrete opt-in official source factories."""

import re
from dataclasses import dataclass

from eie.config.settings import Settings
from eie.core.errors import ValidationError
from eie.ingestion.models import RetentionMode, RetentionPolicy
from eie.ingestion.processors import AdapterProcessor, IngestionProcessor
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.ingestion import OfficialSourceIngestor
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.sec.adapter import SecAdapter, SecIngestor
from eie.providers.sec.models import CIK
from eie.providers.sec.settings import SecSettings
from eie.runtime.clock import RuntimeClock
from eie.runtime.config import positive_int, positive_number
from eie.runtime.sources import HttpPollingSource, PollableSource
from eie.scheduled.index import ScheduledEventIndex
from eie.transport.http import StdlibHttpTransport
from eie.transport.models import HttpPolicy
from eie.transport.protocols import HttpTransport
from eie.transport.rate_limit import RateLimiter, RatePolicy
from eie.transport.retry import RetryingClient, RetryPolicy


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    source: PollableSource
    processor: IngestionProcessor
    enabled: bool = False
    poll_interval: float = 60.0
    stale_after: float = 300.0
    max_payload_bytes: int = 2_000_000
    retention: RetentionPolicy = RetentionPolicy()

    def __post_init__(self) -> None:
        if self.source.provider_id != self.processor.provider_id or not isinstance(
            self.enabled, bool
        ):
            raise ValidationError("matching provider registration required")
        positive_number(self.poll_interval, 86400)
        positive_number(self.stale_after, 604800)
        positive_int(self.max_payload_bytes, 100_000_000)
        if self.stale_after < self.poll_interval or not isinstance(self.retention, RetentionPolicy):
            raise ValidationError("invalid provider staleness/retention policy")
        if (
            self.retention.mode is RetentionMode.BOUNDED_RAW
            and self.retention.max_raw_bytes < self.max_payload_bytes
        ):
            raise ValidationError("retention bound must cover the configured response bound")


def sec_provider(
    cik: CIK,
    user_agent: str,
    clock: RuntimeClock,
    *,
    enabled: bool = False,
    poll_interval: float = 60,
    rate: RatePolicy | None = None,
    transport: HttpTransport | None = None,
) -> ProviderRegistration:
    """One explicitly configured current submissions resource; no history traversal."""
    if (
        not isinstance(cik, CIK)
        or not isinstance(user_agent, str)
        or re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent) is None
    ):
        raise ValidationError("live SEC requires CIK and configured contact User-Agent")
    rate = rate or RatePolicy()
    if not isinstance(rate, RatePolicy):
        raise ValidationError("typed SEC request rate required")
    if rate.capacity / rate.window_seconds > 10 or rate.capacity > 10:
        raise ValidationError("SEC rate must remain within fair-access ceiling")
    url = f"https://data.sec.gov/submissions/CIK{cik.digits}.json"
    policy = HttpPolicy(("data.sec.gov",), user_agent=user_agent)
    client = RetryingClient(
        transport or StdlibHttpTransport(), policy, RetryPolicy(), RateLimiter(rate, clock), clock
    )
    source = HttpPollingSource("SEC_EDGAR", f"submissions:{cik.digits}", url, client)
    processor = AdapterProcessor(
        "sec",
        SecIngestor(Settings(sec=SecSettings(enabled=True))),
        SecAdapter(),
        source_url=url,
        expected_cik=cik,
        clock=clock,
    )
    return ProviderRegistration(
        source, processor, enabled, poll_interval, max(300, poll_interval * 3)
    )


def bls_provider(
    clock: RuntimeClock, *, enabled: bool = False, transport: HttpTransport | None = None
) -> ProviderRegistration:
    """Official existing iCalendar parser; no new extraction or HTML scraping."""
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    policy = HttpPolicy(("www.bls.gov",), content_types=("text/calendar",))
    client = RetryingClient(
        transport or StdlibHttpTransport(),
        policy,
        RetryPolicy(),
        RateLimiter(RatePolicy(1, 60), clock),
        clock,
    )
    source = HttpPollingSource("BLS", "release-calendar", url, client)
    seed = OfficialSourceIngestor(
        ScheduledEventIndex(Settings(official=OfficialProviderSettings(enabled_ids=("BLS",))))
    )
    return ProviderRegistration(
        source, AdapterProcessor("official", seed, BLSAdapter(), clock=clock), enabled, 3600, 10800
    )
