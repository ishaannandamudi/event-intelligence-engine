"""Domain-separated SHA-256 fingerprints of canonical JSON."""

import hashlib

from eie.core.serialization import canonical_json_bytes


def fingerprint(domain: str, value: object) -> str:
    if not domain or "\x00" in domain:
        raise ValueError("invalid fingerprint domain")
    return hashlib.sha256(
        domain.encode("utf-8") + b"\x00" + canonical_json_bytes(value)
    ).hexdigest()
