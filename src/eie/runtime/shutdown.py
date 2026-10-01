"""Portable foreground signal handling with restoration of previous handlers."""

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import current_thread, main_thread
from types import FrameType

from eie.core.errors import ValidationError


@contextmanager
def termination_signals(request_stop: Callable[[], None]) -> Iterator[None]:
    if current_thread() is not main_thread():
        raise ValidationError("foreground signal handling requires main thread")
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def handle(signum: int, frame: FrameType | None) -> None:
        request_stop()

    try:
        for sig in previous:
            signal.signal(sig, handle)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
