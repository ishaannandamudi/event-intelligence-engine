"""Narrow offline parse and optional transport boundaries."""

from enum import StrEnum
from typing import Protocol

from eie.providers.common.models import OfficialSnapshot


class ProviderCapability(StrEnum):
    SCHEDULE_EXTRACT = "SCHEDULE_EXTRACT"
    RELEASE_EXTRACT = "RELEASE_EXTRACT"
    DATE_ONLY = "DATE_ONLY"
    HISTORICAL_FIXTURE = "HISTORICAL_FIXTURE"


class OfficialAdapter(Protocol):
    provider_id: str
    adapter_version: str

    def capabilities(self) -> frozenset[ProviderCapability]: ...

    def parse(self, payload: bytes) -> OfficialSnapshot: ...


class HttpTransport(Protocol):
    def get(self, url: str, *, timeout_seconds: float, user_agent: str) -> bytes: ...
