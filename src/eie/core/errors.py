"""Typed fail-closed errors."""


class EIEError(Exception):
    """Base engine error."""


class ValidationError(EIEError, ValueError):
    """Invalid contract data."""


class TemporalConsistencyError(ValidationError):
    """Invalid temporal order or availability."""


class SchemaCompatibilityError(ValidationError):
    """Unsupported artifact schema."""


class InvalidLifecycleTransition(ValidationError):
    """Forbidden lifecycle transition."""


class PersistenceError(EIEError):
    """Conflicting or missing persisted history."""


class ReplayError(EIEError):
    """Invalid replay operation."""
