"""Explicit-cutoff extraction queries and source-to-claim audit traversal."""

from typing import TypeVar

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.evidence.claims import ProviderClaimObservation
from eie.nlp.audit import TextAudit
from eie.nlp.documents import TextDocument
from eie.nlp.models import ExtractedEventCandidate, ExtractionRun, VerificationResult
from eie.nlp.repository import Artifact, TextRepository
from eie.nlp.schema import RunStatus, UnscheduledFamily
from eie.nlp.spans import EvidenceSpan

T = TypeVar("T", bound=Artifact)


class TextQueries:
    def __init__(self, repository: TextRepository, dataset_id: str, *, limit: int = 10000) -> None:
        repository.evidence._ids("SELECT id FROM te_runs WHERE 0 ORDER BY id", (), limit)
        self.repository, self.dataset_id, self.limit = repository, dataset_id, limit

    def extractions_as_of(
        self,
        known_as_of_ns: int,
        *,
        document_id: str | None = None,
        status: RunStatus | None = None,
    ) -> tuple[ExtractionRun, ...]:
        require_ns(known_as_of_ns)
        clauses, args = ["d.dataset=?", "r.known<=?"], [self.dataset_id, known_as_of_ns]
        if document_id is not None:
            clauses.append("r.document=?")
            args.append(document_id)
        if status is not None:
            clauses.append("r.status=?")
            args.append(status.value)
        ids = self.repository.evidence._ids(
            "SELECT r.id FROM te_runs r JOIN te_documents d ON r.document=d.id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.known,r.id",
            tuple(args),
            self.limit,
        )
        return tuple(self.repository.run(i) for i in ids)

    def documents(self, known_as_of_ns: int) -> tuple[TextDocument, ...]:
        runs = self.extractions_as_of(known_as_of_ns)
        return tuple(self.repository.document(i) for i in sorted({r.document_id for r in runs}))

    def document(self, document_id: str, known_as_of_ns: int) -> TextDocument:
        if not self.extractions_as_of(known_as_of_ns, document_id=document_id):
            raise ValidationError("document has no extraction in the selected dataset/cutoff")
        return self.repository.document(document_id)

    def extraction_run(self, run_id: str, known_as_of_ns: int) -> ExtractionRun:
        require_ns(known_as_of_ns)
        run = self.repository.run(run_id)
        doc = self.repository.document(run.document_id)
        if doc.dataset_id != self.dataset_id or run.available_ns > known_as_of_ns:
            raise ValidationError("extraction run lies outside selected dataset/cutoff")
        return run

    def event_candidates(
        self,
        known_as_of_ns: int,
        *,
        family: UnscheduledFamily | None = None,
        entity_id: str | None = None,
    ) -> tuple[ExtractedEventCandidate, ...]:
        result = {}
        for run in self.extractions_as_of(known_as_of_ns):
            for i in run.candidate_ids:
                candidate = self.repository.artifact(i)
                if not isinstance(candidate, ExtractedEventCandidate):
                    raise ValidationError("candidate membership type mismatch")
                if (family is None or candidate.proposal.family is family) and (
                    entity_id is None or entity_id in candidate.proposal.entity_ids
                ):
                    result[i] = candidate
        if len(result) > self.limit:
            raise ValidationError("candidate query bound exceeded")
        return tuple(result[i] for i in sorted(result))

    def mentions(self, run_id: str, known_as_of_ns: int) -> tuple[Artifact, ...]:
        run = self.extraction_run(run_id, known_as_of_ns)
        ids = (
            *run.entity_mention_ids,
            *run.numeric_mention_ids,
            *run.temporal_mention_ids,
            *run.period_mention_ids,
        )
        if len(ids) > self.limit:
            raise ValidationError("mention query bound exceeded")
        return tuple(self.repository.artifact(i) for i in ids)

    def failed_extractions(self, known_as_of_ns: int) -> tuple[ExtractionRun, ...]:
        return tuple(r for r in self.extractions_as_of(known_as_of_ns) if r.failures)

    def accepted_claims(self, known_as_of_ns: int) -> tuple[ProviderClaimObservation, ...]:
        ids = {
            i
            for r in self.extractions_as_of(known_as_of_ns)
            for a in r.accepted
            for i in a.assertion_ids
        }
        if len(ids) > self.limit:
            raise ValidationError("accepted claim query bound exceeded")
        return tuple(self.repository.evidence.assertion(i) for i in sorted(ids))

    def _members(self, ids: tuple[str, ...], cls: type[T]) -> tuple[T, ...]:
        result = []
        for i in ids:
            value = self.repository.artifact(i)
            if not isinstance(value, cls):
                raise ValidationError("audit membership type mismatch")
            result.append(value)
        return tuple(result)

    def audit(self, run_id: str, known_as_of_ns: int) -> TextAudit:
        with self.repository.store.transaction():
            run = self.extraction_run(run_id, known_as_of_ns)
            doc = self.document(run.document_id, known_as_of_ns)
            candidates = self._members(run.candidate_ids, ExtractedEventCandidate)
            verifications = self._members(run.verification_ids, VerificationResult)
            spans = self._members(run.span_ids, EvidenceSpan)
            assertion_ids = sorted(
                {i for a in run.accepted for i in a.assertion_ids}
                | set(run.retracted_assertion_ids)
            )
            assertions = tuple(self.repository.evidence.assertion(i) for i in assertion_ids)
            evidence_ids = sorted({i for a in assertions for i in a.evidence_ids})
            return TextAudit(
                doc,
                run,
                self.repository.mapping(run.map_id),
                tuple(self.repository.segment(i) for i in run.segment_ids),
                self.mentions(run_id, known_as_of_ns),
                tuple(candidates),
                tuple(verifications),
                tuple(spans),
                assertions,
                tuple(self.repository.evidence.reference(i) for i in evidence_ids),
            )

    def event_audits(self, event_id: str, known_as_of_ns: int) -> tuple[TextAudit, ...]:
        return tuple(
            self.audit(r.run_id, known_as_of_ns)
            for r in self.extractions_as_of(known_as_of_ns)
            if any(a.event_id == event_id for a in r.accepted)
        )
