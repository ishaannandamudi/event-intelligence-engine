"""Stable sanitized failures; no external exception text."""

from eie.core.errors import EIEError
from eie.service.schema import ServiceCode


class ServiceError(EIEError):
    def __init__(self, code: ServiceCode) -> None:
        self.code = code
        super().__init__(code.value)
