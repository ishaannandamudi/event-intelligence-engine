"""Real standard-library HTTP, pinned validated DNS, bounded reads and redirects."""

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from threading import BoundedSemaphore, Event, Thread
from typing import Protocol
from urllib.parse import urljoin

from eie.core.errors import ValidationError
from eie.transport.errors import (
    ResponseLimitError,
    TransientTransportError,
    TransportPolicyError,
)
from eie.transport.models import SAFE_RESPONSE_HEADERS, HttpPolicy, HttpResponse


class Response(Protocol):
    status: int

    def getheader(self, name: str) -> str | None: ...
    def read(self, amount: int) -> bytes: ...


class Connection(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None: ...
    def getresponse(self) -> Response: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[str, str, int, HttpPolicy], Connection]
_DNS_SLOTS = BoundedSemaphore(8)


def resolve_addresses(host: str, port: int, timeout: float) -> tuple[str, ...]:
    """Bound OS DNS waits and abandoned lookups to eight concurrent daemon threads."""
    deadline = time.monotonic() + timeout
    if not _DNS_SLOTS.acquire(timeout=timeout):
        raise TimeoutError("resolver capacity exhausted")
    done = Event()
    addresses: list[str] = []
    errors: list[OSError] = []

    def resolve() -> None:
        try:
            addresses.extend(
                str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        except OSError as exc:
            errors.append(exc)
        finally:
            _DNS_SLOTS.release()
            done.set()

    Thread(target=resolve, name="eie-dns", daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        raise TimeoutError("provider DNS timed out")
    if errors:
        raise OSError("provider DNS unavailable") from errors[0]
    return tuple(addresses)


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, scheme: str, host: str, port: int, policy: HttpPolicy) -> None:
        super().__init__(host, port, timeout=policy.connect_timeout)
        self._scheme, self._policy = scheme, policy

    def connect(self) -> None:
        deadline = time.monotonic() + self._policy.connect_timeout
        addresses = resolve_addresses(self.host, self.port, self._policy.connect_timeout)
        if not addresses:
            raise OSError("no provider address")
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global and not (
                parsed.is_loopback and self._policy.allow_http_loopback
            ):
                raise TransportPolicyError("provider DNS resolved to a prohibited address")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("connect deadline exceeded")
        self.sock = socket.create_connection((addresses[0], self.port), remaining)
        try:
            if self._scheme == "https":
                self.sock = ssl.create_default_context().wrap_socket(
                    self.sock, server_hostname=self.host
                )
            self.sock.settimeout(self._policy.read_timeout)
        except BaseException:
            self.sock.close()
            raise


class StdlibHttpTransport:
    def __init__(self, connection_factory: ConnectionFactory = _PinnedConnection) -> None:
        self._factory = connection_factory

    def fetch(
        self, url: str, policy: HttpPolicy, conditionals: tuple[tuple[str, str], ...] = ()
    ) -> HttpResponse:
        headers = {"User-Agent": policy.user_agent, "Accept-Encoding": "identity"}
        for key, value in conditionals:
            if (
                key not in ("If-None-Match", "If-Modified-Since")
                or not value
                or any(c in value for c in "\r\n\x00")
            ):
                raise TransportPolicyError("unsafe request header")
            headers[key] = value
        for redirect in range(policy.max_redirects + 1):
            scheme, host, port, path = policy.validate_url(url)
            connection = self._factory(scheme, host, port, policy)
            try:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if redirect == policy.max_redirects or not location:
                        raise TransportPolicyError("redirect exceeds configured policy")
                    target = urljoin(url, location)
                    policy.validate_url(target)
                    if host != policy.validate_url(target)[1]:
                        headers = {k: v for k, v in headers.items() if not k.startswith("If-")}
                    url = target
                    continue
                metadata = tuple(
                    (key, header_value)
                    for key in sorted(SAFE_RESPONSE_HEADERS)
                    if (header_value := response.getheader(key)) is not None
                )
                encoding = response.getheader("Content-Encoding")
                if encoding is not None and encoding.lower() != "identity":
                    raise TransportPolicyError("compressed responses are unsupported")
                length = response.getheader("Content-Length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise TransportPolicyError("invalid response length")
                    if len(length) > 12 or int(length) > policy.max_response_bytes:
                        raise ResponseLimitError("response exceeds configured byte limit")
                if response.status == 200:
                    content_type = (
                        (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
                    )
                    if content_type not in policy.content_types:
                        raise TransportPolicyError("unexpected provider content type")
                chunks: list[bytes] = []
                size = 0
                deadline = time.monotonic() + policy.read_timeout
                while response.status != 304:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = response.read(min(65536, policy.max_response_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > policy.max_response_bytes:
                        raise ResponseLimitError("response exceeds configured byte limit")
                    chunks.append(chunk)
                return HttpResponse(response.status, metadata, b"".join(chunks))
            except ValidationError as exc:
                raise TransportPolicyError("invalid provider response metadata") from exc
            except ssl.SSLCertVerificationError as exc:
                raise TransportPolicyError("provider TLS certificate validation failed") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransientTransportError("bounded HTTP acquisition failed") from exc
            finally:
                connection.close()
        raise TransportPolicyError("redirect policy exhausted")
