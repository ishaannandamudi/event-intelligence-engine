"""Recalculate every component from immutable pinned inputs and policy."""

from eie.core.errors import EIEError
from eie.intelligence.collection import collect
from eie.intelligence.engine import calculate
from eie.intelligence.models import IntelligenceValidation
from eie.intelligence.policy import DEFAULT_INTELLIGENCE_LIMITS, IntelligenceLimits
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.schema import ValidationState


def validate(
    repository: IntelligenceRepository,
    identity: str,
    *,
    limits: IntelligenceLimits = DEFAULT_INTELLIGENCE_LIMITS,
) -> IntelligenceValidation:
    try:
        with repository.store.transaction():
            value = repository.get(identity)
            if value is None:
                return IntelligenceValidation(
                    identity, ValidationState.INVALID, ("RESULT_NOT_FOUND",)
                )
            for ref in value.manifest.current:
                if ref.kind.value == "RESOLUTION":
                    resolution = repository.evidence.stored_resolution(ref.identity)
                    from eie.intelligence.collection import reference

                    if (
                        reference(ref.kind, ref.identity, resolution, ref.known_ns).fingerprint
                        != ref.fingerprint
                    ):
                        return IntelligenceValidation(
                            identity, ValidationState.INVALID, ("RESOLUTION_REFERENCE_MISMATCH",)
                        )
            policy = repository.policy(value.manifest.request.policy_id)
            data = collect(
                repository.store,
                value.manifest.request,
                policy,
                limits,
                select_event_resolution=False,
            )
            expected = calculate(data, policy, limits)
            if expected != value:
                return IntelligenceValidation(
                    identity, ValidationState.INVALID, ("SOURCE_COMPONENT_RECALCULATION_MISMATCH",)
                )
            return IntelligenceValidation(
                identity,
                ValidationState.WARN if value.quality else ValidationState.VALID,
                value.quality,
            )
    except EIEError:
        return IntelligenceValidation(
            identity, ValidationState.INVALID, ("INVALID_RETAINED_INPUT_OR_ARTIFACT",)
        )
