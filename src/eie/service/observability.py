"""Bounded operational telemetry outside semantic snapshot identity."""

import logging
from collections import Counter
from threading import RLock

from eie.core.fingerprints import fingerprint
from eie.core.serialization import canonical_json_bytes
from eie.service import API_VERSION
from eie.service.schema import ServiceCode
from eie.version import __version__


class OperationalMetrics:
    def __init__(self) -> None:
        self._lock = RLock()
        self._counts: Counter[str] = Counter()
        self._durations: Counter[str] = Counter()
        self.logger = logging.getLogger("eie.service")

    def record(
        self,
        operation: str,
        outcome: str,
        duration_ns: int = 0,
        *,
        request_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> None:
        if operation not in (
            "snapshot",
            "assembly",
            "batch",
            "expansion",
            "validation",
            "readiness",
            "cache_hit",
            "cache_miss",
            "db_lock",
        ):
            raise ValueError("fixed operational category required")
        if outcome not in {"OK", *(c.value for c in ServiceCode)}:
            raise ValueError("fixed operational outcome required")
        if snapshot_id is not None and (
            not snapshot_id.startswith("eies_")
            or len(snapshot_id) != 69
            or any(c not in "0123456789abcdef" for c in snapshot_id[5:])
        ):
            raise ValueError("safe snapshot identity required")
        with self._lock:
            self._counts[operation] += 1
            self._durations[operation] += max(0, duration_ns)
            if outcome != "OK":
                self._counts["failures"] += 1
        entry = {
            "operation": operation,
            "outcome": outcome,
            "duration_ns": max(0, duration_ns),
            "snapshot_id": snapshot_id,
            "correlation_hash": None
            if request_id is None
            else fingerprint("eie:service:correlation:v1", request_id),
        }
        self.logger.info("%s", canonical_json_bytes(entry).decode())

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "engine_version": __version__,
                "api_version": API_VERSION,
                "counts": dict(sorted(self._counts.items())),
                "duration_ns": dict(sorted(self._durations.items())),
            }
