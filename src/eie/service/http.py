"""Optional explicit standard-library local server with one bounded worker."""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import IPv4Address

from eie.core.errors import ValidationError
from eie.service.api import ServiceApi
from eie.service.models import ServiceFailure
from eie.service.schema import ServiceCode
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class HttpSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    connection_timeout: float = 5.0
    backlog: int = 16

    def __post_init__(self) -> None:
        contract(self)
        try:
            local = IPv4Address(self.host).is_loopback
        except ValueError:
            local = False
        if (
            not local
            or not 1 <= self.port <= 65535
            or not 0 < self.connection_timeout <= 60
            or not 1 <= self.backlog <= 128
        ):
            raise ValidationError("bounded local HTTP settings required")


def handler(api: ServiceApi, settings: HttpSettings) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "EIE/1.0"
        sys_version = ""

        def setup(self) -> None:
            self.request.settimeout(settings.connection_timeout)
            super().setup()

        def log_message(self, format: str, *args: object) -> None:
            # Base access logs contain paths; operational logging uses fixed categories.
            pass

        def do_GET(self) -> None:
            self.process()

        def do_POST(self) -> None:
            self.process()

        def process(self) -> None:
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1:
                    raise ValueError("unsupported framing")
                length = 0 if not lengths else int(lengths[0])
                if length < 0 or length > api.provider.limits.max_request_bytes:
                    status, output = 413, api.response(ServiceFailure(ServiceCode.BOUND_EXCEEDED))
                else:
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError("incomplete request")
                    status, output = api.dispatch(
                        self.command, self.path, body, request_id=self.headers.get("X-Request-ID")
                    )
            except (ValueError, TimeoutError, OSError):
                status, output = 400, api.response(ServiceFailure(ServiceCode.INVALID_REQUEST))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(output)))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(output)
            self.close_connection = True

    return Handler


DEFAULT_HTTP_SETTINGS = HttpSettings()


def serve(api: ServiceApi, settings: HttpSettings = DEFAULT_HTTP_SETTINGS) -> None:
    class BoundedServer(HTTPServer):
        request_queue_size = settings.backlog

    with BoundedServer((settings.host, settings.port), handler(api, settings)) as server:
        server.serve_forever(poll_interval=0.25)
