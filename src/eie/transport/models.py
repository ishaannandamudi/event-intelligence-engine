"""Explicit HTTP policy and bounded response contracts."""

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from eie.core.errors import ValidationError
from eie.ingestion.models import label
from eie.runtime.config import positive_int, positive_number
from eie.transport.errors import TransportPolicyError

SAFE_RESPONSE_HEADERS = frozenset(
    ("content-type", "content-length", "content-encoding", "etag", "last-modified", "retry-after")
)


def valid_host(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(ipaddress.ip_address(value)) == value
    except ValueError:
        return re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", value) is not None


@dataclass(frozen=True, slots=True)
class HttpPolicy:
    allowed_hosts: tuple[str, ...]
    user_agent: str = field(default="EIE/0.7", repr=False)
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_response_bytes: int = 2_000_000
    max_redirects: int = 0
    content_types: tuple[str, ...] = ("application/json",)
    allow_http_loopback: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_hosts, tuple)
            or not self.allowed_hosts
            or any(not valid_host(host) for host in self.allowed_hosts)
            or len(set(self.allowed_hosts)) != len(self.allowed_hosts)
        ):
            raise ValidationError("explicit lowercase provider hosts required")
        label(self.user_agent)
        if len(self.user_agent) > 512 or not self.user_agent.isascii():
            raise ValidationError("User-Agent exceeds bound")
        positive_number(self.connect_timeout, 120)
        positive_number(self.read_timeout, 120)
        positive_int(self.max_response_bytes, 100_000_000)
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 5
            or not isinstance(self.allow_http_loopback, bool)
            or not isinstance(self.content_types, tuple)
            or not self.content_types
            or any(
                not isinstance(c, str) or re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", c) is None
                for c in self.content_types
            )
        ):
            raise ValidationError("invalid HTTP redirect/content policy")

    def validate_url(self, url: str) -> tuple[str, str, int, str]:
        try:
            if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (
                host not in self.allowed_hosts
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.query
                or parsed.scheme not in ("https", "http")
            ):
                raise ValueError
            loopback = host == "localhost"
            try:
                address = ipaddress.ip_address(host)
                loopback = address.is_loopback
                if not address.is_global and not (loopback and self.allow_http_loopback):
                    raise ValueError
            except ValueError:
                if re.fullmatch(r"[0-9.]+|.*:.*", host):
                    raise
            if parsed.scheme == "http" and not (loopback and self.allow_http_loopback):
                raise ValueError
            port = (
                parsed.port
                if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80)
            )
            if not 1 <= port <= 65535:
                raise ValueError
            return parsed.scheme, host, port, parsed.path or "/"
        except ValueError as exc:
            raise TransportPolicyError("request violates configured endpoint policy") from exc


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = field(default=b"", repr=False)
    attempts: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 100 <= self.status <= 599
        ):
            raise ValidationError("invalid HTTP status")
        positive_int(self.attempts, 20)
        if not isinstance(self.body, bytes) or not isinstance(self.headers, tuple):
            raise ValidationError("typed HTTP response required")
        if len({k for k, _ in self.headers}) != len(self.headers):
            raise ValidationError("duplicate response metadata")
        for key, value in self.headers:
            if key not in SAFE_RESPONSE_HEADERS:
                raise ValidationError("response header not allowlisted")
            label(value)
            if len(value) > 4096:
                raise ValidationError("response header exceeds bound")

    def header(self, name: str) -> str | None:
        return dict(self.headers).get(name.lower())
