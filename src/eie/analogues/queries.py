"""Bounded indexed histories and explicit descriptive explanation APIs."""

from eie.analogues.models import (
    AnalogueCandidate,
    AnalogueResult,
    AnalogueRunManifest,
    AnalogueUncertainty,
    MarketReactionSurprise,
    ReactionDistribution,
)
from eie.analogues.repository import AnalogueRepository
from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.persistence.codec import dumps


class AnalogueQueries:
    def __init__(self, repository: AnalogueRepository) -> None:
        self.repository = repository

    def analogues_as_of(
        self,
        as_of_ns: int,
        *,
        event_id: str | None = None,
        dataset_id: str | None = None,
        policy_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AnalogueResult, ...]:
        require_ns(as_of_ns)
        where = ["known<=?", "available<=?"]
        parameters: list[object] = [as_of_ns, as_of_ns]
        for column, value in (("target", event_id), ("dataset", dataset_id), ("policy", policy_id)):
            if value is not None:
                where.append(column + "=?")
                parameters.append(value)
        repo = self.repository
        with repo.store.transaction():
            ids = repo.evidence._ids(
                "SELECT id FROM analogue_results WHERE "
                + " AND ".join(where)
                + " ORDER BY known,id",
                tuple(parameters),
                limit,
            )
            results = []
            output_bytes = 0
            for identity in ids:
                result = repo.get(identity)
                if (
                    result is None
                    or result.query.known_as_of_ns > as_of_ns
                    or result.query.available_ns > as_of_ns
                ):
                    raise PersistenceError("analogue query index leaked unavailable result")
                results.append(result)
                output_bytes += len(dumps(result))
                if output_bytes > repo.limits.query_output_bytes:
                    raise ValidationError("analogue history output bound exceeded")
            return tuple(results)

    def analogues(self, identity: str) -> AnalogueResult | None:
        return self.repository.get(identity)

    def analogue_distribution(self, identity: str) -> ReactionDistribution | None:
        result = self.repository.get(identity)
        return None if result is None else result.distribution

    def analogue_uncertainty(self, identity: str) -> tuple[AnalogueUncertainty, ...]:
        result = self.repository.get(identity)
        return () if result is None else result.uncertainty

    def candidate_explanations(self, identity: str) -> tuple[AnalogueCandidate, ...]:
        result = self.repository.get(identity)
        return () if result is None else result.candidates

    def reaction_surprise(self, identity: str) -> MarketReactionSurprise | None:
        return self.repository.reaction(identity)

    def run(self, identity: str) -> AnalogueRunManifest | None:
        return self.repository.run(identity)
