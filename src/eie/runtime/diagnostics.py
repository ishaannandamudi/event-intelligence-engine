"""Lightweight operational counters and sanitized structured logging."""

import logging
from collections import Counter
from threading import Lock

from eie.ingestion.models import FailureClass, PollResult

LOGGER = logging.getLogger("eie.runtime")


class RuntimeMetrics:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def record(self, result: PollResult) -> None:
        with self._lock:
            self._counts["polls_total"] += 1
            self._counts["polls_failed"] += int(result.error_category is not None)
            self._counts["items_fetched"] += result.items_fetched
            self._counts["items_ingested"] += result.items_ingested
            self._counts["duplicates_suppressed"] += result.duplicates
            self._counts["items_quarantined"] += int(result.quarantined)
            self._counts["ingestion_latency_ns"] += result.latencies.total_ns
            if result.error_category is not None:
                self._counts[result.error_category.value.lower()] += 1
                self._counts["provider_failures"] += 1
                self._counts["normalization_failures"] += int(
                    result.error_category
                    in (
                        FailureClass.NORMALIZATION_FAILURE,
                        FailureClass.SCHEMA_MISMATCH,
                        FailureClass.PARSE_FAILURE,
                    )
                )
                self._counts["persistence_failures"] += int(
                    result.error_category
                    in (
                        FailureClass.PERSISTENCE_FAILURE,
                        FailureClass.CHECKPOINT_FAILURE,
                    )
                )
                self._counts["rate_limits"] += int(result.error_category is FailureClass.RATE_LIMIT)

    def snapshot(self, queue_depth: int) -> dict[str, int]:
        with self._lock:
            return {**self._counts, "queue_depth": queue_depth}


def log_result(result: PollResult) -> None:
    LOGGER.info(
        "provider_poll",
        extra={
            "provider": result.provider_id,
            "operation": "poll",
            "status": "committed" if result.committed else "failed",
            "error_category": None
            if result.error_category is None
            else result.error_category.value,
            "duration_ns": result.latencies.total_ns,
            "checkpoint_generation": result.checkpoint_generation,
        },
    )
