"""Append-only compact source index; normal metric queries avoid upstream validation."""

from typing import TypeVar

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.evidence.repository import EvidenceRepository
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability import inputs
from eie.reliability.models import (
    Artifact,
    BitemporalAuditResult,
    CoverageAssessment,
    CoverageUniverse,
    InformationLatency,
    ProcessingTiming,
    ReliabilityQuery,
    SourceHistoryRow,
    SourceReliabilityResult,
)
from eie.reliability.schema import SubjectType, WindowKind

T = TypeVar("T", bound=Artifact)


class ReliabilityRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store, self.evidence = store, EvidenceRepository(store)

    @staticmethod
    def indexes(row: SourceHistoryRow) -> tuple[object, ...]:
        return (
            row.artifact_id,
            row.dataset_id,
            row.kind.value,
            row.subject_id,
            row.thread_id,
            row.source_revision_id,
            row.source.name,
            row.resource_id,
            row.source_family,
            row.event_type,
            row.field,
            row.scope_id,
            row.number,
            row.known_ns,
            row.metric_available_ns,
            row.sample_available_ns,
            None if row.clocks.publication is None else row.clocks.publication.timestamp_ns,
            None if row.clocks.observed is None else row.clocks.observed.timestamp_ns,
            row.fingerprint,
        )

    def append_row(self, row: SourceHistoryRow) -> SourceHistoryRow:
        self.evidence.owned()
        if self.store._rows("SELECT e.id FROM reliability_rows e WHERE id=?", (row.artifact_id,)):
            if self.row(row.artifact_id) != row:
                raise PersistenceError("immutable source projection disagreement")
            return row
        self.evidence.dataset(row.dataset_id)
        self.validate_source(row)
        if self.store._rows(
            "SELECT 1 FROM reliability_seals WHERE dataset=? AND "
            "((mode='audit' AND cutoff>=?) OR (mode='metric' AND cutoff>?)) LIMIT 1",
            (row.dataset_id, row.metric_available_ns, row.metric_available_ns),
        ):
            raise ValidationError("cannot backdate source index into sealed historical query")
        self.evidence._put("reliability_rows", self.indexes(row), row)
        return row

    def index_claim(
        self, assertion_id: str, timing: ProcessingTiming | None = None
    ) -> SourceHistoryRow:
        with self.store.transaction():
            assertion = self.evidence.assertion(assertion_id)
            base = inputs.from_claim(self.evidence, assertion)
            row = inputs.from_claim(self.evidence, assertion, timing)
            if timing is not None:
                self.put(timing)
            if row.metric_available_ns > base.metric_available_ns:
                self.append_row(base)
            return self.append_row(row)

    def index_event(
        self,
        dataset: str,
        event_id: str,
        known: int,
        resource: str,
        family: str,
        raw_id: str,
        timing: ProcessingTiming | None = None,
    ) -> SourceHistoryRow:
        with self.store.transaction():
            event = self.store.get_event(event_id, known)
            if event is None:
                raise ValidationError("visible event required for source diagnostics")
            base = inputs.from_event(self.evidence, dataset, event, resource, family, raw_id)
            row = inputs.from_event(self.evidence, dataset, event, resource, family, raw_id, timing)
            if timing is not None:
                self.put(timing)
            if row.metric_available_ns > base.metric_available_ns:
                self.append_row(base)
            return self.append_row(row)

    def index_observation(
        self,
        dataset: str,
        raw_id: str,
        resource: str,
        family: str,
        timing: ProcessingTiming | None = None,
    ) -> SourceHistoryRow:
        with self.store.transaction():
            raw = self.store.get_observation(raw_id)
            if raw is None:
                raise ValidationError("retained raw observation required")
            base = inputs.from_observation(dataset, raw, resource, family)
            row = inputs.from_observation(dataset, raw, resource, family, timing)
            if timing is not None:
                self.put(timing)
            if row.metric_available_ns > base.metric_available_ns:
                self.append_row(base)
            return self.append_row(row)

    @staticmethod
    def latest_frame(cutoff: int, *, inclusive: bool = False) -> str:
        require_ns(cutoff)
        operator = "<=" if inclusive else "<"
        return (
            "NOT EXISTS (SELECT 1 FROM reliability_rows newer WHERE newer.dataset=e.dataset "
            "AND newer.kind=e.kind AND newer.provider=e.provider AND newer.resource=e.resource "
            "AND newer.source_revision=e.source_revision AND newer.available>e.available "
            f"AND newer.available{operator}{cutoff})"
        )

    def row(self, identity: str, *, deep: bool = False) -> SourceHistoryRow:
        item = self.evidence._one("reliability_rows", identity, SourceHistoryRow)
        if item is None or item[1] != self.indexes(item[0]):
            raise PersistenceError("missing/corrupt reliability source index")
        if deep:
            self.validate_source(item[0])
        return item[0]

    def validate_source(self, row: SourceHistoryRow) -> None:
        timing = None if row.timing_id is None else self.artifact(row.timing_id, ProcessingTiming)
        if row.kind is SubjectType.CLAIM:
            rebuilt = inputs.from_claim(
                self.evidence, self.evidence.assertion(row.source_revision_id), timing
            )
        elif row.kind is SubjectType.EVENT:
            event = next(
                (
                    e
                    for e in self.store.get_revisions(row.subject_id)
                    if e.revision_id == row.source_revision_id
                ),
                None,
            )
            if event is None:
                raise PersistenceError("missing canonical event revision")
            rebuilt = inputs.from_event(
                self.evidence,
                row.dataset_id,
                event,
                row.resource_id,
                row.source_family,
                row.raw_id,
                timing,
            )
        else:
            raw = self.store.get_observation(row.raw_id)
            if raw is None:
                raise PersistenceError("missing source observation")
            rebuilt = inputs.from_observation(
                row.dataset_id, raw, row.resource_id, row.source_family, timing
            )
        if rebuilt != row:
            raise PersistenceError("source projection does not reconcile with retained owner")

    @staticmethod
    def artifact_indexes(value: Artifact) -> tuple[object, ...]:
        dataset = provider = resource = subject = policy = None
        known = 0
        if isinstance(value, SourceReliabilityResult):
            q = value.query
            dataset, provider, resource, subject, known, policy = (
                q.dataset_id,
                q.provider,
                q.resource_id,
                q.field,
                q.known_as_of_ns,
                q.policy.artifact_id,
            )
        elif isinstance(value, BitemporalAuditResult):
            dataset, subject, known = value.dataset_id, value.subject_id, value.known_as_of_ns
        elif isinstance(value, InformationLatency):
            subject, known = value.row_id, value.known_as_of_ns
        elif isinstance(value, CoverageUniverse):
            dataset, provider, resource, subject, known = (
                value.dataset_id,
                value.provider,
                value.resource_id,
                value.scope_id,
                value.known_ns,
            )
        elif isinstance(value, ProcessingTiming):
            subject, known = value.subject_id, value.available_ns
        elif hasattr(value, "known_as_of_ns"):
            known = value.known_as_of_ns
        return (
            value.artifact_id,
            type(value).__name__,
            dataset,
            provider,
            resource,
            subject,
            known,
            policy,
            value.fingerprint,
        )

    def put(self, value: Artifact) -> None:
        self.evidence.owned()
        self.evidence._put("reliability_artifacts", self.artifact_indexes(value), value)

    def artifact(self, identity: str, cls: type[T]) -> T:
        item = self.evidence._one("reliability_artifacts", identity, cls)
        if item is None or item[1] != self.artifact_indexes(item[0]):
            raise PersistenceError("missing/corrupt reliability artifact")
        return item[0]

    def seal(self, dataset: str, cutoff: int, mode: str = "metric") -> None:
        self.evidence.owned()
        self.store._connection.execute(
            "INSERT OR IGNORE INTO reliability_seals VALUES (?,?,?)", (dataset, cutoff, mode)
        )

    def ids(self, sql: str, args: tuple[object, ...], maximum: int) -> tuple[str, ...]:
        return self.evidence._ids(sql, args, maximum)

    def collect(
        self, query: ReliabilityQuery
    ) -> tuple[tuple[SourceHistoryRow, ...], tuple[SourceHistoryRow, ...]]:
        clauses = [
            "dataset=?",
            "kind=?",
            "provider=?",
            "resource=?",
            "sample_available<?",
            "available<?",
        ]
        args: list[object] = [
            query.dataset_id,
            query.kind.value,
            query.provider,
            query.resource_id,
            query.known_as_of_ns,
            query.known_as_of_ns,
        ]
        for column, value in (
            ("event_type", query.event_type),
            ("field", query.field),
            ("scope", query.scope_id),
        ):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        if query.policy.window is WindowKind.TRAILING_DAYS:
            assert query.policy.days is not None
            clauses.append("sample_available>=?")
            args.append(max(0, query.known_as_of_ns - query.policy.days * 86_400_000_000_000))
        clauses.append(self.latest_frame(query.known_as_of_ns))
        where = " AND ".join(clauses)
        if query.policy.window is WindowKind.TRAILING_OBSERVATIONS:
            assert query.policy.observations is not None
            boundary = self.store._rows(
                "SELECT sample_available FROM reliability_rows e WHERE "
                + where
                + " ORDER BY sample_available DESC LIMIT 1 OFFSET ?",
                tuple(args) + (query.policy.observations - 1,),
            )
            if boundary:
                clauses.append("sample_available>=?")
                args.append(boundary[0][0])
                where = " AND ".join(clauses)
        row_ids = self.ids(
            "SELECT e.id FROM reliability_rows e WHERE " + where + " ORDER BY id",
            tuple(args),
            query.policy.max_observations,
        )
        rows = tuple(self.row(i) for i in row_ids)
        predecessor_ids = tuple(
            sorted({r.previous_source_id for r in rows if r.previous_source_id is not None})
        )
        if predecessor_ids:
            ids = self.ids(
                "SELECT e.id FROM reliability_rows e WHERE dataset=? AND kind=? AND "
                "provider=? AND resource=? AND sample_available<? AND available<? "
                "AND source_revision IN ("
                + ",".join("?" for _ in predecessor_ids)
                + ") AND "
                + self.latest_frame(query.known_as_of_ns)
                + " ORDER BY e.id",
                tuple(args[:6]) + predecessor_ids,
                query.policy.max_observations,
            )
            rows = tuple(
                sorted(
                    {r.artifact_id: r for r in (*rows, *(self.row(i) for i in ids))}.values(),
                    key=lambda r: r.artifact_id,
                )
            )
        subjects = tuple(sorted({r.subject_id for r in rows}))
        peers: tuple[SourceHistoryRow, ...] = ()
        if subjects and query.kind is SubjectType.CLAIM:
            ids = self.ids(
                "SELECT e.id FROM reliability_rows e WHERE dataset=? AND kind='CLAIM' "
                "AND sample_available<? AND available<? AND thread IN ("
                "SELECT thread FROM reliability_rows WHERE dataset=? AND kind='CLAIM' "
                "AND sample_available<? AND available<? AND subject IN ("
                + ",".join("?" for _ in subjects)
                + ")) AND "
                + self.latest_frame(query.known_as_of_ns)
                + " ORDER BY e.id",
                (
                    query.dataset_id,
                    query.known_as_of_ns,
                    query.known_as_of_ns,
                    query.dataset_id,
                    query.known_as_of_ns,
                    query.known_as_of_ns,
                    *subjects,
                ),
                query.policy.max_observations,
            )
            peers = tuple(self.row(i) for i in ids)
        return rows, peers

    def append_universe(self, universe: CoverageUniverse) -> str:
        with self.store.transaction():
            self.evidence.dataset(universe.dataset_id)
            if not self.store._rows(
                "SELECT id FROM reliability_artifacts WHERE id=?", (universe.artifact_id,)
            ):
                self._coverage_unsealed(universe.dataset_id, universe.known_ns)
            self.put(universe)
        return universe.artifact_id

    def _coverage_unsealed(self, dataset: str, known: int) -> None:
        if self.store._rows(
            "SELECT 1 FROM reliability_seals WHERE dataset=? AND mode='metric' "
            "AND cutoff>? LIMIT 1",
            (dataset, known),
        ):
            raise ValidationError("cannot backdate coverage into sealed source history")

    def append_assessment(self, assessment: CoverageAssessment) -> str:
        with self.store.transaction():
            universe = self.artifact(assessment.universe_id, CoverageUniverse)
            if assessment.known_ns < universe.known_ns or assessment.slot_key not in {
                s.key for s in universe.slots
            }:
                raise ValidationError("coverage assessment precedes/breaches pinned universe")
            if assessment.row_id is not None:
                row = self.row(assessment.row_id)
                slot = next(s for s in universe.slots if s.key == assessment.slot_key)
                if (
                    row.dataset_id,
                    row.source.name,
                    row.resource_id,
                    row.scope_id,
                    row.event_type,
                ) != (
                    universe.dataset_id,
                    universe.provider,
                    universe.resource_id,
                    slot.scope_id,
                    slot.event_type,
                ) or row.metric_available_ns > assessment.known_ns:
                    raise ValidationError(
                        "coverage observed row attribution/availability disagreement"
                    )
            if not self.store._rows(
                "SELECT id FROM reliability_assessments WHERE id=?", (assessment.artifact_id,)
            ):
                self._coverage_unsealed(universe.dataset_id, assessment.known_ns)
            self.evidence._put(
                "reliability_assessments",
                (
                    assessment.artifact_id,
                    assessment.universe_id,
                    assessment.slot_key,
                    assessment.known_ns,
                    assessment.fingerprint,
                ),
                assessment,
            )
        return assessment.artifact_id

    def assessments(
        self, universe: str, cutoff: int, maximum: int
    ) -> tuple[CoverageAssessment, ...]:
        ids = self.ids(
            "SELECT id FROM reliability_assessments WHERE universe=? AND known<? ORDER BY id",
            (universe, cutoff),
            maximum,
        )
        result = []
        for identity in ids:
            item = self.evidence._one("reliability_assessments", identity, CoverageAssessment)
            if item is None or item[1] != (
                item[0].artifact_id,
                item[0].universe_id,
                item[0].slot_key,
                item[0].known_ns,
                item[0].fingerprint,
            ):
                raise PersistenceError("coverage assessment index disagreement")
            result.append(item[0])
        return tuple(result)
