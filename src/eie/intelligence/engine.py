"""Pure component assembly and atomic snapshot calculation/persistence."""

from typing import TYPE_CHECKING

from eie.core.errors import ValidationError
from eie.evidence.repository import EvidenceRepository
from eie.intelligence.collection import collect
from eie.intelligence.inputs import IntelligenceInputs
from eie.intelligence.materiality import calculate_materiality, importance
from eie.intelligence.models import EventIntelligenceResult, IntelligenceRequest
from eie.intelligence.novelty import calculate_novelty, entity_ids
from eie.intelligence.policy import (
    DEFAULT_INTELLIGENCE_LIMITS,
    IntelligenceLimits,
    IntelligencePolicy,
)
from eie.intelligence.schema import IntelligenceMode
from eie.intelligence.uncertainty import decompose


def calculate(
    data: IntelligenceInputs,
    policy: IntelligencePolicy,
    limits: IntelligenceLimits = DEFAULT_INTELLIGENCE_LIMITS,
) -> EventIntelligenceResult:
    novelty = calculate_novelty(data, policy)
    materiality = calculate_materiality(data, novelty, policy)
    uncertainty = decompose(data, materiality)
    if (
        len(novelty) > limits.components
        or len(materiality.components) > limits.components
        or any(len(c.reasons) > limits.uncertainty_reasons for c in uncertainty)
    ):
        raise ValidationError("intelligence component/reason bound exceeded")
    quality = set(materiality.reasons)
    if not data.prior:
        quality.add("NO_PRIOR_FACT_HISTORY")
    if not data.documents:
        quality.add("DOCUMENT_NOVELTY_NOT_AVAILABLE")
    if any(c.state.value == "UNRESOLVED" for c in novelty):
        quality.add("UNRESOLVED_INFORMATION")
    return EventIntelligenceResult(
        data.manifest,
        data.event.revision_id,
        data.event.identity.event_type.code,
        data.event.identity.event_type.family.value,
        entity_ids(data.current),
        policy.fingerprint,
        novelty,
        importance(data, policy),
        materiality,
        uncertainty,
        tuple(sorted(quality)),
        IntelligenceMode.EX_POST_AUDIT if data.impacts else IntelligenceMode.EX_ANTE_INTELLIGENCE,
    )


class IntelligenceEngine:
    def __init__(
        self,
        repository: "IntelligenceRepository",
        *,
        limits: IntelligenceLimits = DEFAULT_INTELLIGENCE_LIMITS,
    ) -> None:
        self.repository, self.limits = repository, limits

    def compute(self, request: IntelligenceRequest) -> EventIntelligenceResult:
        repo = self.repository
        with repo.store.transaction():
            policy = repo.policy(request.policy_id)
            data = collect(repo.store, request, policy, self.limits)
            result = calculate(data, policy, self.limits)
            evidence = EvidenceRepository(repo.store)
            repo.persist_resolutions(data, self.limits)
            repo._append(result, data)
            evidence.seal(request.dataset_id, request.known_as_of_ns)
            return result


if TYPE_CHECKING:
    from eie.intelligence.repository import IntelligenceRepository
