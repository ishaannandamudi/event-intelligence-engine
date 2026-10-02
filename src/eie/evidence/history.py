"""Cutoff-aware knowledge intervals and immutable grouping history."""

from dataclasses import dataclass

from eie.core.time import require_ns
from eie.evidence.claims import ProviderClaimObservation
from eie.evidence.resolution import unique_observations
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class ClaimKnowledgeInterval:
    assertion: ProviderClaimObservation
    known_to_ns: int | None

    def __post_init__(self) -> None:
        contract(self)


def knowledge_intervals(
    rows: tuple[ProviderClaimObservation, ...], known_as_of_ns: int
) -> tuple[ClaimKnowledgeInterval, ...]:
    require_ns(known_as_of_ns)
    visible = sorted(
        unique_observations(tuple(r for r in rows if r.known_from_ns <= known_as_of_ns)),
        key=lambda r: (r.known_from_ns, r.thread_id, r.number, r.observation_id),
    )
    return tuple(
        ClaimKnowledgeInterval(
            r,
            min(
                (
                    n.known_from_ns
                    for n in visible
                    if n.thread_id == r.thread_id and n.number > r.number
                ),
                default=None,
            ),
        )
        for r in visible
    )
