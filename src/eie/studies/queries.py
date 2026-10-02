"""Typed indexed selection and descriptive study queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eie.backfill.models import ReconstructionQuality
from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.events.taxonomy import EventFamily
from eie.studies.aggregation import DistributionQuery, ResponseDistribution, historical_distribution
from eie.studies.checks import contract
from eie.studies.decay import EventDecayResult, class_decay, decay
from eie.studies.models import EventStudyResult, ResponsePoint

if TYPE_CHECKING:
    from eie.studies.repository import StudyRepository


@dataclass(frozen=True, slots=True)
class EventStudyFilter:
    event_types: tuple[str, ...] = ()
    families: frozenset[EventFamily] = frozenset()
    issuer_id: str | None = None
    start_ns: int = 0
    end_ns: int = 2**63 - 1
    require_surprise: bool = False
    reconstruction_quality: ReconstructionQuality | None = None
    exclude_contaminated: bool = False

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.start_ns)
        require_ns(self.end_ns)
        if self.start_ns >= self.end_ns or self.event_types != tuple(sorted(set(self.event_types))):
            raise ValidationError("invalid study filter range/types")

    def matches(self, result: EventStudyResult) -> bool:
        d = result.definition
        return (
            (not self.event_types or d.event.identity.event_type.code in self.event_types)
            and (not self.families or d.event.identity.event_type.family in self.families)
            and (self.issuer_id is None or self.issuer_id in d.event.identity.scope.identifiers)
            and self.start_ns <= result.anchor_ns < self.end_ns
            and (
                not self.require_surprise
                or d.surprise is not None
                and d.surprise.normalized_surprise is not None
            )
            and (
                self.reconstruction_quality is None
                or d.reconstruction_quality is self.reconstruction_quality
            )
            and (not self.exclude_contaminated or not result.contamination)
        )


DEFAULT_FILTER = EventStudyFilter()


class StudyQueries:
    def __init__(self, repository: StudyRepository) -> None:
        self.repository = repository

    def response_curve(self, study_id: str) -> tuple[ResponsePoint, ...]:
        r = self.repository.get(study_id)
        if r is None:
            raise ValidationError("study unavailable")
        return r.curve

    def studies_as_of(
        self, as_of_ns: int, build_id: str, filters: EventStudyFilter = DEFAULT_FILTER
    ) -> tuple[EventStudyResult, ...]:
        return tuple(
            r
            for r in self.repository.studies_as_of(as_of_ns, build_id=build_id)
            if filters.matches(r)
        )

    def historical_distribution(self, query: DistributionQuery) -> ResponseDistribution:
        return historical_distribution(
            self.repository.studies_as_of(
                query.as_of_ns, build_id=query.event_build_id, event_type=query.event_type_code
            ),
            query,
        )

    def decay(self, study_id: str) -> EventDecayResult:
        r = self.repository.get(study_id)
        if r is None:
            raise ValidationError("study unavailable")
        return decay(r)

    def class_decay(self, query: DistributionQuery) -> EventDecayResult:
        return class_decay(
            self.repository.studies_as_of(
                query.as_of_ns, build_id=query.event_build_id, event_type=query.event_type_code
            ),
            query,
        )
