"""Provider boundary error categories."""

from eie.core.errors import EIEError


class ProviderError(EIEError):
    pass


class ProviderTransportError(ProviderError):
    pass


class ProviderUnavailableError(ProviderTransportError):
    pass


class ProviderRateLimitedError(ProviderTransportError):
    pass


class ProviderParseError(ProviderError):
    pass


class ProviderSchemaMismatchError(ProviderParseError):
    pass


class ProviderNormalizationError(ProviderError):
    pass
