"""Real HTTP parser over fake sockets plus deterministic retry/rate policy tests."""

import io
import socket
import ssl
from dataclasses import replace
from threading import BoundedSemaphore, Event

import pytest

from eie.core.errors import ValidationError
from eie.runtime.clock import ManualClock
from eie.transport import http
from eie.transport.errors import (
    AcquisitionCancelled,
    ResponseLimitError,
    TransientTransportError,
    TransportPolicyError,
)
from eie.transport.http import StdlibHttpTransport
from eie.transport.models import HttpPolicy, HttpResponse
from eie.transport.rate_limit import RateLimiter, RatePolicy
from eie.transport.retry import RetryingClient, RetryPolicy, retry_after

URL = "https://provider.example/current"
POLICY = HttpPolicy(("provider.example",))


class Response:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self.body, self.position = status, body, 0
        self.headers = headers if headers is not None else {"content-type": "application/json"}
        self.reads = []

    def getheader(self, key):
        return self.headers.get(key.lower())

    def read(self, amount):
        self.reads.append(amount)
        value = self.body[self.position : self.position + amount]
        self.position += len(value)
        return value


class Connection:
    def __init__(self, response, failure=None):
        self.response, self.failure, self.closed = response, failure, False
        self.headers = {}

    def request(self, method, path, *, headers):
        assert method == "GET"
        self.headers, self.path = headers, path
        if self.failure is not None:
            raise self.failure

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def transport(*responses):
    connections = [Connection(r) for r in responses]
    remaining = iter(connections)
    return StdlibHttpTransport(lambda *args: next(remaining)), connections


@pytest.mark.parametrize("status", (200, 304, 404, 429, 500))
def test_bounded_http_statuses(status):
    client, connections = transport(
        Response(
            status,
            headers={"content-type": "application/json", "etag": '"v1"', "set-cookie": "private"},
        )
    )
    result = client.fetch(URL, POLICY, (("If-None-Match", '"v0"'),))
    assert result.status == status
    assert result.body == (b"" if status == 304 else b"{}")
    assert result.header("ETag") == '"v1"'
    assert result.header("set-cookie") is None
    assert connections[0].closed
    assert connections[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "headers,body,error",
    (
        ({"content-type": "text/html"}, b"<html>", TransportPolicyError),
        (
            {"content-type": "application/json", "content-length": "abc"},
            b"{}",
            TransportPolicyError,
        ),
        ({"content-type": "application/json", "content-length": "100"}, b"{}", ResponseLimitError),
        (
            {"content-type": "application/json", "content-encoding": "gzip"},
            b"{}",
            TransportPolicyError,
        ),
        ({"content-type": "application/json"}, b"123456", ResponseLimitError),
        ({}, b"{}", TransportPolicyError),
    ),
)
def test_response_limits_content_and_compression(headers, body, error):
    client, connections = transport(Response(body=body, headers=headers))
    with pytest.raises(error):
        client.fetch(URL, replace(POLICY, max_response_bytes=5))
    assert connections[0].closed


def test_empty_and_exact_limit_response():
    for body in (b"", b"12345"):
        client, _ = transport(
            Response(body=body, headers={"content-type": "application/json; charset=utf-8"})
        )
        assert client.fetch(URL, replace(POLICY, max_response_bytes=5)).body == body


@pytest.mark.parametrize(
    "location",
    (
        "https://evil.example/item",
        "http://provider.example/item",
        "file:///tmp/x",
        "/item?token=secret",
    ),
)
def test_unsafe_redirect(location):
    client, connections = transport(Response(302, headers={"location": location}))
    with pytest.raises(TransportPolicyError):
        client.fetch(URL, replace(POLICY, max_redirects=1))
    assert connections[0].closed


def test_safe_redirect_and_budget():
    client, connections = transport(Response(302, headers={"location": "/new"}), Response())
    assert client.fetch(URL, replace(POLICY, max_redirects=1)).body == b"{}"
    assert connections[1].path == "/new" and all(c.closed for c in connections)
    client, _ = transport(Response(302, headers={"location": "/new"}))
    with pytest.raises(TransportPolicyError):
        client.fetch(URL, POLICY)
    client, connections = transport(
        Response(302, headers={"location": "https://other.example/new"}), Response()
    )
    client.fetch(
        URL,
        replace(POLICY, allowed_hosts=("provider.example", "other.example"), max_redirects=1),
        (("If-None-Match", '"x"'),),
    )
    assert "If-None-Match" not in connections[1].headers
    client, _ = transport(Response(302, headers={}))
    with pytest.raises(TransportPolicyError):
        client.fetch(URL, replace(POLICY, max_redirects=1))


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/x",
        "ftp://provider.example/x",
        "data:text/plain,x",
        "http://provider.example/x",
        "https://evil.example/x",
        "https://user:secret@provider.example/x",
        "https://provider.example/x?api_key=private",
        "https://provider.example/x#part",
        "https://provider.example:0/x",
        "https://provider.example:bad/x",
        "https://provider.example/a\nb",
        "https://127.0.0.1/x",
        "https://10.0.0.1/x",
        "https://[::1]/x",
    ),
)
def test_url_policy(url):
    hosts = ("provider.example", "127.0.0.1", "10.0.0.1", "::1")
    with pytest.raises(TransportPolicyError):
        HttpPolicy(hosts).validate_url(url)


def test_explicit_loopback_and_public_literal():
    assert (
        HttpPolicy(("localhost",), allow_http_loopback=True).validate_url(
            "http://localhost:8123/x"
        )[2]
        == 8123
    )
    assert (
        HttpPolicy(("127.0.0.1",), allow_http_loopback=True).validate_url("http://127.0.0.1/x")[0]
        == "http"
    )
    assert HttpPolicy(("8.8.8.8",)).validate_url("https://8.8.8.8/")[3] == "/"


@pytest.mark.parametrize(
    "header", (("Authorization", "private"), ("If-None-Match", "x\ny"), ("If-Modified-Since", ""))
)
def test_no_auth_or_injected_request_headers(header):
    client, _ = transport(Response())
    with pytest.raises(TransportPolicyError):
        client.fetch(URL, POLICY, (header,))


@pytest.mark.parametrize(
    "failure,error",
    (
        (TimeoutError(), TransientTransportError),
        (OSError(), TransientTransportError),
        (ssl.SSLCertVerificationError(), TransportPolicyError),
    ),
)
def test_transport_timeout_connection_and_tls(failure, error):
    connection = Connection(Response(), failure)
    client = StdlibHttpTransport(lambda *args: connection)
    with pytest.raises(error):
        client.fetch(URL, POLICY)
    assert connection.closed


class FakeSocket:
    def __init__(self):
        self.data = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        self.closed = False
        self.sent = b""
        self.timeouts = []

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args):
        return self.data

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("scheme", ("https", "http"))
def test_real_stdlib_http_parser_and_dns_pinning(monkeypatch, scheme):
    sock = FakeSocket()
    destinations = []
    tls_hosts = []
    monkeypatch.setattr(http, "resolve_addresses", lambda *args: ("8.8.8.8",))

    def dial(address, timeout):
        destinations.append(address)
        assert 0 < timeout <= 5
        return sock

    monkeypatch.setattr(socket, "create_connection", dial)

    class Context:
        def wrap_socket(self, s, *, server_hostname):
            tls_hosts.append(server_hostname)
            return s

    monkeypatch.setattr(ssl, "create_default_context", Context)
    policy = HttpPolicy(("localhost",), allow_http_loopback=True) if scheme == "http" else POLICY
    url = "http://localhost/current" if scheme == "http" else URL
    result = StdlibHttpTransport().fetch(url, policy)
    assert result.body == b"{}" and destinations[0][0] == "8.8.8.8"
    assert b"GET /current HTTP/1.1" in sock.sent
    assert sock.timeouts == [10] and sock.closed
    assert tls_hosts == ([] if scheme == "http" else ["provider.example"])


@pytest.mark.parametrize("addresses", ((), ("127.0.0.1",), ("8.8.8.8", "10.0.0.1")))
def test_dns_safety(monkeypatch, addresses):
    monkeypatch.setattr(http, "resolve_addresses", lambda *args: addresses)
    with pytest.raises((TransientTransportError, TransportPolicyError)):
        StdlibHttpTransport().fetch(URL, POLICY)


def test_dns_resolution_error_and_bounded_capacity(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("8.8.8.8", 443))]
    )
    assert http.resolve_addresses("provider.example", 443, 0.5) == ("8.8.8.8",)

    def fail(*args, **kwargs):
        raise OSError()

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(OSError):
        http.resolve_addresses("provider.example", 443, 0.5)
    release = Event()
    finished = Event()

    def blocked(*args, **kwargs):
        release.wait(1)
        finished.set()
        return []

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(http, "_DNS_SLOTS", BoundedSemaphore(1))
    try:
        with pytest.raises(TimeoutError):
            http.resolve_addresses("provider.example", 443, 0.001)
        with pytest.raises(TimeoutError):
            http.resolve_addresses("provider.example", 443, 0.001)
    finally:
        release.set()
        assert finished.wait(1)


class ScriptTransport:
    def __init__(self, *values):
        self.values = values
        self.calls = 0
        self.conditionals = []

    def fetch(self, url, policy, conditionals=()):
        self.conditionals.append(conditionals)
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value


def client(source, clock=None, retry=None, rate=None):
    clock = clock or ManualClock(1_000_000_000)
    return RetryingClient(
        source,
        POLICY,
        retry or RetryPolicy(),
        RateLimiter(rate or RatePolicy(100, 1), clock),
        clock,
    )


def test_retry_transient_and_finite_exponential_backoff():
    source = ScriptTransport(
        TransientTransportError("temporary"), HttpResponse(500), HttpResponse(200)
    )
    value = client(source)
    assert value.fetch(URL, Event()).attempts == 3
    assert value.clock.monotonic_ns() == 1_500_000_000 and source.calls == 3
    source = ScriptTransport(TransientTransportError("temporary"))
    value = client(source)
    with pytest.raises(TransientTransportError):
        value.fetch(URL, Event())
    assert source.calls == 3


def test_retry_status_and_nonretryable_policy():
    for status in (404, 304, 200):
        source = ScriptTransport(HttpResponse(status))
        value = client(source)
        assert value.fetch(URL, Event()).status == status and source.calls == 1
    source = ScriptTransport(TransportPolicyError("unsafe"))
    value = client(source)
    with pytest.raises(TransportPolicyError):
        value.fetch(URL, Event())
    assert source.calls == 1
    source = ScriptTransport(HttpResponse(429, (("retry-after", "10"),)))
    value = client(source, retry=RetryPolicy(3, 0.5, 2))
    assert value.fetch(URL, Event()).attempts == 3 and source.calls == 3
    assert value.clock.monotonic_ns() == 4_000_000_000
    assert value.limiter.state().observed_429 == 3 and value.limiter.state().throttled


def test_redirects_cannot_bypass_per_request_rate_budget():
    source = ScriptTransport(HttpResponse(200))
    clock = ManualClock()
    with pytest.raises(ValidationError):
        RetryingClient(
            source,
            HttpPolicy(("example.com",), max_redirects=1),
            RetryPolicy(),
            RateLimiter(RatePolicy(1, 60), clock),
            clock,
        )
    assert source.calls == 0


@pytest.mark.parametrize(
    "value,expected",
    (
        (None, None),
        ("2", 2),
        ("999999999999999999999", 5),
        ("invalid", None),
        ("-1", None),
        ("Thu, 01 Jan 1970 00:00:03 GMT", 2),
        ("Thu, 01 Jan 1970 00:00:00 GMT", 0),
        ("Thu, 01 Jan 1970 00:00:03", 2),
    ),
)
def test_retry_after(value, expected):
    assert retry_after(value, 1_000_000_000, 5) == expected


def test_rate_isolation_window_and_stop():
    clock = ManualClock()
    one = RateLimiter(RatePolicy(2, 1), clock)
    two = RateLimiter(RatePolicy(1, 1), clock)
    one.acquire(Event())
    one.acquire(Event())
    assert one.state().throttled and not two.state().throttled
    one.acquire(Event())
    assert clock.monotonic_ns() == 1_000_000_000 and not one.state().throttled
    stop = Event()
    stop.set()
    with pytest.raises(AcquisitionCancelled):
        one.acquire(stop)
    with pytest.raises(AcquisitionCancelled):
        client(ScriptTransport(HttpResponse(200))).fetch(URL, stop)


def test_retry_stop_during_wait():
    stop = Event()

    class StoppingClock(ManualClock):
        def wait(self, seconds, event):
            event.set()
            return True

    value = client(ScriptTransport(HttpResponse(500)), clock=StoppingClock())
    with pytest.raises(AcquisitionCancelled):
        value.fetch(URL, stop)


@pytest.mark.parametrize(
    "changes",
    (
        {"allowed_hosts": ()},
        {"allowed_hosts": ("Upper.example",)},
        {"allowed_hosts": ("x", "x")},
        {"user_agent": "bad\nheader"},
        {"user_agent": "x" * 513},
        {"connect_timeout": 0},
        {"read_timeout": float("nan")},
        {"max_response_bytes": True},
        {"max_response_bytes": 0},
        {"max_redirects": -1},
        {"max_redirects": True},
        {"allow_http_loopback": 1},
        {"content_types": ()},
        {"content_types": ("bad",)},
    ),
)
def test_http_policy_configuration(changes):
    with pytest.raises(ValidationError):
        replace(POLICY, **changes)


@pytest.mark.parametrize(
    "constructor",
    (
        lambda: RetryPolicy(0),
        lambda: RetryPolicy(11),
        lambda: RetryPolicy(2, 3, 2),
        lambda: RatePolicy(0),
        lambda: RatePolicy(1, -1),
        lambda: HttpResponse(True),
        lambda: HttpResponse(200, body="text"),
        lambda: HttpResponse(200, (("authorization", "secret"),)),
        lambda: HttpResponse(200, (("etag", "a"), ("etag", "b"))),
        lambda: HttpResponse(200, (("etag", "x" * 4097),)),
    ),
)
def test_typed_response_and_retry_validation(constructor):
    with pytest.raises(ValidationError):
        constructor()
