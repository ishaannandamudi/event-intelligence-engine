"""Bounded queue: rejection is explicit and leaves the checkpoint untouched."""

from dataclasses import dataclass
from queue import Empty, Full, Queue

from eie.runtime.config import positive_int
from eie.runtime.sources import AcquiredPayload


@dataclass(frozen=True, slots=True)
class QueueItem:
    provider_id: str
    acquired: AcquiredPayload
    observed_ns: int
    fetch_started_mono_ns: int
    fetched_mono_ns: int
    enqueued_mono_ns: int


class IngestionQueue:
    def __init__(self, capacity: int) -> None:
        positive_int(capacity, 10_000)
        self._queue: Queue[QueueItem] = Queue(capacity)

    def offer(self, item: QueueItem) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except Full:
            return False

    def take(self, timeout: float = 0) -> QueueItem | None:
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def complete(self) -> None:
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()
