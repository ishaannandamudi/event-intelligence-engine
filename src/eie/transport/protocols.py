"""Acquisition interface supports deterministic fakes without network access."""

from typing import Protocol

from eie.transport.models import HttpPolicy, HttpResponse


class HttpTransport(Protocol):
    def fetch(
        self, url: str, policy: HttpPolicy, conditionals: tuple[tuple[str, str], ...] = ()
    ) -> HttpResponse: ...
