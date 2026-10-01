"""Artifact schemas are versioned independently of the Python package."""

EVENT_SCHEMA_VERSION = "1.0"
OBSERVATION_SCHEMA_VERSION = "1.0"
ENTITY_SCHEMA_VERSION = "1.0"
CONFIG_SCHEMA_VERSION = "1.0"


def require_schema(actual: str, expected: str) -> None:
    from eie.core.errors import SchemaCompatibilityError

    if actual != expected:
        raise SchemaCompatibilityError(f"expected schema {expected}, got {actual}")
