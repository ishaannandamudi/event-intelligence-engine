"""Liveness has no data-quality or provider-outage meaning."""

from eie.service.models import HealthResult


def health() -> HealthResult:
    return HealthResult()
