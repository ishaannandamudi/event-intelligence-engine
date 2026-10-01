"""Safe transport errors: messages never contain URLs, headers, or payloads."""

from eie.core.errors import EIEError


class TransportError(EIEError):
    """Acquisition failed."""


class TransientTransportError(TransportError):
    """Timeout or temporary connection failure; eligible for bounded retry."""


class TransportPolicyError(TransportError):
    """Unsafe request or incompatible response; never automatically retried."""


class ResponseLimitError(TransportPolicyError):
    """Response exceeds configured limit."""


class AcquisitionCancelled(TransportError):
    """Runtime stopped before acquisition could finish."""
