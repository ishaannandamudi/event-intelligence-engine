"""Canonical wire views include computed IDs without circular semantic hashes."""

from dataclasses import fields, is_dataclass

from eie.service.models import EIESnapshot


def wire(value: object) -> object:
    if isinstance(value, EIESnapshot):
        return {
            **{f.name: wire(getattr(value, f.name)) for f in fields(value)},
            "snapshot_id": value.snapshot_id,
            "fingerprint": value.fingerprint,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: wire(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return tuple(wire(v) for v in value)
    return value
