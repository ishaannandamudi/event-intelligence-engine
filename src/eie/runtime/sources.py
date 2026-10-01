"""Polling contracts and explicit HTTP/fixture source implementations."""

from dataclasses import dataclass
from threading import Event
from typing import Protocol

from eie.core.errors import ValidationError
from eie.ingestion.models import Checkpoint, Cursor, identifier, label
from eie.transport.models import HttpResponse
from eie.transport.retry import RetryingClient


@dataclass(frozen=True, slots=True)
class AcquiredPayload:
    response: HttpResponse
    cursor: Cursor | None = None


class PollableSource(Protocol):
    provider_id: str
    resource_id: str

    def poll(self, checkpoint: Checkpoint | None, stop: Event) -> AcquiredPayload: ...


class HttpPollingSource:
    def __init__(
        self, provider_id: str, resource_id: str, url: str, client: RetryingClient
    ) -> None:
        identifier(provider_id)
        label(resource_id)
        client.policy.validate_url(url)
        self.provider_id, self.resource_id = provider_id, resource_id
        self.url, self.client = url, client

    def poll(self, checkpoint: Checkpoint | None, stop: Event) -> AcquiredPayload:
        headers = (
            ()
            if checkpoint is None
            else tuple(
                (key, value)
                for key, value in (
                    ("If-None-Match", checkpoint.etag),
                    ("If-Modified-Since", checkpoint.last_modified),
                )
                if value is not None
            )
        )
        return AcquiredPayload(self.client.fetch(self.url, stop, headers))


class FixtureSource:
    """Explicit offline poll source; final payload repeats to exercise overlap."""

    def __init__(
        self, provider_id: str, resource_id: str, responses: tuple[AcquiredPayload, ...]
    ) -> None:
        identifier(provider_id)
        label(resource_id)
        if (
            not isinstance(responses, tuple)
            or not responses
            or any(not isinstance(r, AcquiredPayload) for r in responses)
        ):
            raise ValidationError("nonempty typed fixture responses required")
        self.provider_id, self.resource_id = provider_id, resource_id
        self._responses, self.polls = responses, 0

    def poll(self, checkpoint: Checkpoint | None, stop: Event) -> AcquiredPayload:
        from eie.transport.errors import AcquisitionCancelled

        if stop.is_set():
            raise AcquisitionCancelled("fixture source stopped")
        result = self._responses[min(self.polls, len(self._responses) - 1)]
        self.polls += 1
        return result
