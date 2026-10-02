"""Static typed offline interchange, bounded before decoding."""

from eie.core.errors import ValidationError
from eie.providers.evidence.models import EvidenceBatch


def parse(payload: bytes, *, max_bytes: int = 8_000_000) -> EvidenceBatch:
    from eie.persistence.codec import loads

    if (
        not isinstance(payload, bytes)
        or isinstance(max_bytes, bool)
        or not 1 <= max_bytes <= 100_000_000
        or len(payload) > max_bytes
    ):
        raise ValidationError("bounded structured evidence bytes required")
    value = loads(payload)
    if not isinstance(value, EvidenceBatch):
        raise ValidationError("structured evidence interchange required")
    return value
