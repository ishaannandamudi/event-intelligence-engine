"""Normal compact checks and explicit owner/dependency audit."""

from eie.core.errors import EIEError
from eie.core.serialization import canonical_json_bytes
from eie.service.assembly import assemble
from eie.service.models import ValidationResult
from eie.service.repository import ServiceRepository
from eie.service.schema import ValidationDepth
from eie.service.serialization import wire


def _validate(
    repository: ServiceRepository,
    snapshot_id: str,
    depth: ValidationDepth = ValidationDepth.COMPACT,
) -> ValidationResult:
    try:
        value = repository.snapshot(snapshot_id)
        if value is None or assemble(repository, value.request) != value:
            return ValidationResult(snapshot_id, depth, False, ("SNAPSHOT_RECOLLECTION_MISMATCH",))
        if len(canonical_json_bytes(wire(value))) > value.request.limits.max_snapshot_bytes:
            return ValidationResult(snapshot_id, depth, False, ("SNAPSHOT_BOUND_EXCEEDED",))
        if depth is ValidationDepth.DEEP:
            for identity in sorted(
                {p for c in value.components for p in c.publication_ids}
                | set(value.identity.reference_ids)
            ):
                repository.publication(identity, deep=True)
        return ValidationResult(snapshot_id, depth, True)
    except EIEError:
        return ValidationResult(snapshot_id, depth, False, ("INTERNAL_VALIDATION_FAILURE",))


def validate(
    repository: ServiceRepository,
    snapshot_id: str,
    depth: ValidationDepth = ValidationDepth.COMPACT,
) -> ValidationResult:
    try:
        with repository.store.transaction():
            return _validate(repository, snapshot_id, depth)
    except EIEError:
        return ValidationResult(snapshot_id, depth, False, ("INTERNAL_VALIDATION_FAILURE",))
