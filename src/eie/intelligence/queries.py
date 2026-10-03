"""Bounded historical queries; result availability and baseline knowledge are distinct."""

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.intelligence.models import (
    EventIntelligenceResult,
    EventMateriality,
    NoveltyComponent,
    UncertaintyComponent,
)
from eie.intelligence.policy import DEFAULT_INTELLIGENCE_LIMITS, IntelligenceLimits
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.schema import (
    MaterialityCategory,
    NoveltyDimension,
    UncertaintyKind,
    UncertaintyState,
)


class IntelligenceQueries:
    def __init__(
        self,
        repository: IntelligenceRepository,
        dataset_id: str,
        *,
        limits: IntelligenceLimits = DEFAULT_INTELLIGENCE_LIMITS,
    ) -> None:
        self.repository, self.dataset_id, self.limits = repository, dataset_id, limits

    def history(
        self,
        known_as_of_ns: int,
        *,
        event_id: str | None = None,
        family: str | None = None,
        event_type: str | None = None,
        entity_id: str | None = None,
        source_family: str | None = None,
        measurement: str | None = None,
        policy_id: str | None = None,
        materiality: MaterialityCategory | None = None,
        uncertainty_kind: UncertaintyKind | None = None,
        uncertainty_state: UncertaintyState | None = None,
        start_ns: int = 0,
    ) -> tuple[EventIntelligenceResult, ...]:
        require_ns(known_as_of_ns)
        require_ns(start_ns)
        if start_ns > known_as_of_ns or (uncertainty_kind is None) != (uncertainty_state is None):
            raise ValidationError(
                "ordered history interval and complete uncertainty filter required"
            )
        clauses, args = (
            ["r.dataset=?", "r.known>=?", "r.known<=?", "r.available<=?"],
            [self.dataset_id, start_ns, known_as_of_ns, known_as_of_ns],
        )
        for column, value in (
            ("event", event_id),
            ("family", family),
            ("event_type", event_type),
            ("policy", policy_id),
            ("materiality", None if materiality is None else materiality.value),
        ):
            if value is not None:
                clauses.append("r." + column + "=?")
                args.append(value)
        for table, column, value in (
            ("ei_entities", "entity", entity_id),
            ("ei_sources", "family", source_family),
            ("ei_measurements", "measurement", measurement),
        ):
            if value is not None:
                clauses.append(
                    f"EXISTS(SELECT 1 FROM {table} m WHERE m.result=r.id AND m.{column}=?)"
                )
                args.append(value)
        if uncertainty_kind is not None and uncertainty_state is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM ei_uncertainty u WHERE u.result=r.id AND u.kind=? "
                "AND u.state=?)"
            )
            args.extend((uncertainty_kind.value, uncertainty_state.value))
        with self.repository.store.transaction():
            ids = self.repository.evidence._ids(
                "SELECT r.id FROM ei_results r WHERE "
                + " AND ".join(clauses)
                + " ORDER BY r.known,r.id",
                tuple(args),
                self.limits.query_size,
            )
            results = tuple(self.repository.get(i) for i in ids)
            if any(r is None for r in results):
                raise PersistenceError("intelligence query index references missing result")
            return tuple(r for r in results if r is not None)

    def intelligence_as_of(
        self, event_id: str, known_as_of_ns: int
    ) -> EventIntelligenceResult | None:
        values = self.history(known_as_of_ns, event_id=event_id)
        return values[-1] if values else None

    def novelty(
        self, event_id: str, known_as_of_ns: int, dimension: NoveltyDimension | None = None
    ) -> tuple[NoveltyComponent, ...]:
        value = self.intelligence_as_of(event_id, known_as_of_ns)
        return (
            ()
            if value is None
            else tuple(c for c in value.novelty if dimension is None or c.dimension is dimension)
        )

    def materiality(self, event_id: str, known_as_of_ns: int) -> EventMateriality | None:
        value = self.intelligence_as_of(event_id, known_as_of_ns)
        return None if value is None else value.materiality

    def uncertainty(self, event_id: str, known_as_of_ns: int) -> tuple[UncertaintyComponent, ...]:
        value = self.intelligence_as_of(event_id, known_as_of_ns)
        return () if value is None else value.uncertainty

    def prior_distribution(
        self, event_id: str, known_as_of_ns: int, claim_id: str | None = None
    ) -> tuple[NoveltyComponent, ...]:
        return tuple(
            c
            for c in self.novelty(event_id, known_as_of_ns, NoveltyDimension.NUMERIC)
            if claim_id is None or c.subject_id == claim_id
        )
