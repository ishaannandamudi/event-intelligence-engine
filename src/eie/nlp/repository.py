"""Indexed immutable text artifacts, content entitlement and complete run memberships."""

from hashlib import sha256
from typing import TypeVar

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.repository import EvidenceRepository, typed
from eie.ingestion.models import RetentionMode, RetentionPermission
from eie.nlp.documents import TextDocument
from eie.nlp.entities import EntityCatalog
from eie.nlp.extraction import ExtractionArtifacts
from eie.nlp.mentions import EntityMention, NumericMention, PeriodMention, TemporalMention
from eie.nlp.models import ExtractedEventCandidate, ExtractionRun, VerificationResult
from eie.nlp.normalization import NormalizationMap
from eie.nlp.prompts import PromptTemplate
from eie.nlp.segmentation import TextSegment
from eie.nlp.spans import EvidenceSpan
from eie.persistence.sqlite import SQLiteEventStore

Artifact = (
    EntityMention
    | NumericMention
    | PeriodMention
    | TemporalMention
    | ExtractedEventCandidate
    | VerificationResult
    | EvidenceSpan
)
T = TypeVar("T")
ARTIFACT_TYPES: tuple[tuple[str, type[Artifact]], ...] = (
    ("ENTITY", EntityMention),
    ("NUMERIC", NumericMention),
    ("PERIOD", PeriodMention),
    ("TEMPORAL", TemporalMention),
    ("CANDIDATE", ExtractedEventCandidate),
    ("VERIFICATION", VerificationResult),
    ("SPAN", EvidenceSpan),
)


def artifact_identity(value: Artifact) -> tuple[str, str]:
    if isinstance(value, ExtractedEventCandidate):
        return "CANDIDATE", value.candidate_id
    if isinstance(value, VerificationResult):
        return "VERIFICATION", value.verification_id
    if isinstance(value, EvidenceSpan):
        return "SPAN", value.span_id
    kind = next(k for k, t in ARTIFACT_TYPES if isinstance(value, t))
    return kind, value.mention_id


class TextRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store
        self.evidence = EvidenceRepository(store)

    def _read(
        self, table: str, value_id: str, cls: type[T], expected: tuple[object, ...] | None = None
    ) -> T:
        result = self.evidence._one(table, value_id, cls)
        if result is None:
            raise PersistenceError("missing text extraction artifact")
        value, scalars = result
        if expected is not None and scalars != expected:
            raise PersistenceError("text artifact index mismatch")
        return value

    @staticmethod
    def document_scalars(value: TextDocument) -> tuple[object, ...]:
        return (
            value.document_id,
            value.dataset_id,
            value.raw.observation_id,
            value.thread_id,
            value.raw.payload_hash,
            value.raw.published_at_ns,
            value.raw.source_observed_at_ns,
        )

    def document(self, value_id: str) -> TextDocument:
        value = self._read("te_documents", value_id, TextDocument)
        self._read("te_documents", value_id, TextDocument, self.document_scalars(value))
        if value.document_id != value_id:
            raise PersistenceError("document identity mismatch")
        self.evidence.validate_lineage(value.source_lineage, value.dataset_id)
        return value

    def content(self, document_id: str) -> bytes | None:
        doc = self.document(document_id)
        if doc.retention.mode is not RetentionMode.BOUNDED_RAW:
            return None
        if doc.retention.permission is not RetentionPermission.ALLOWED:
            raise PersistenceError("stored content lacks entitlement")
        rows = self.store._rows(
            "SELECT content FROM te_contents WHERE id=?", (doc.raw.payload_hash,)
        )
        if not rows or not isinstance(rows[0][0], bytes):
            raise PersistenceError("missing entitled source content")
        payload = rows[0][0]
        if (
            len(payload) > doc.retention.max_raw_bytes
            or sha256(payload).hexdigest() != doc.raw.payload_hash
        ):
            raise PersistenceError("retained content hash/bound mismatch")
        return payload

    def mapping(self, value_id: str) -> NormalizationMap:
        value = self._read("te_maps", value_id, NormalizationMap)
        self._read(
            "te_maps",
            value_id,
            NormalizationMap,
            (value.map_id, value.original_hash, value.normalized_hash),
        )
        if value.map_id != value_id:
            raise PersistenceError("normalization identity mismatch")
        return value

    def catalog(self, value_id: str) -> EntityCatalog:
        value = self._read("te_catalogs", value_id, EntityCatalog, (value_id,))
        if value.fingerprint != value_id:
            raise PersistenceError("entity catalog identity mismatch")
        return value

    def template(self, value_id: str) -> PromptTemplate:
        value = self._read("te_templates", value_id, PromptTemplate)
        self._read(
            "te_templates",
            value_id,
            PromptTemplate,
            (value.fingerprint, value.template_id, value.version),
        )
        if value.fingerprint != value_id:
            raise PersistenceError("prompt template identity mismatch")
        return value

    def segment(self, value_id: str) -> TextSegment:
        value = self._read("te_segments", value_id, TextSegment)
        self._read(
            "te_segments",
            value_id,
            TextSegment,
            (
                value.segment_id,
                value.document_id,
                value.map_id,
                value.index,
                value.start,
                value.end,
            ),
        )
        mapping = self.mapping(value.map_id)
        doc = self.document(value.document_id)
        if (
            value.segment_id != value_id
            or value.end > mapping.normalized_characters
            or mapping.original_hash != doc.raw.payload_hash
        ):
            raise PersistenceError("segment document mapping mismatch")
        return value

    def _artifact_scalars(
        self, value: Artifact, doc_id: str, map_id: str, segments: tuple[TextSegment, ...]
    ) -> tuple[object, ...]:
        kind, value_id = artifact_identity(value)
        segment_id, family = None, None
        if isinstance(value, ExtractedEventCandidate):
            if value.document_id != doc_id:
                raise PersistenceError("candidate document mismatch")
            family = value.proposal.family.value
            segment_id = next(
                (
                    s.segment_id
                    for s in segments
                    if s.start == value.proposal.span.start and s.end == value.proposal.span.end
                ),
                None,
            )
        elif isinstance(value, VerificationResult):
            c = self.artifact(value.candidate_id)
            if not isinstance(c, ExtractedEventCandidate) or c.document_id != doc_id:
                raise PersistenceError("verification candidate document mismatch")
        else:
            span = value if isinstance(value, EvidenceSpan) else value.span
            if span.document_id != doc_id or span.map_id != map_id:
                raise PersistenceError("mention/span document mapping mismatch")
            segment_id = span.segment_id
            selected = next((s for s in segments if s.segment_id == segment_id), None)
            mapping = self.mapping(map_id)
            if (
                selected is None
                or not selected.start <= span.start < span.end <= selected.end
                or mapping.original_range(span.start, span.end)
                != (span.original_start, span.original_end)
            ):
                raise PersistenceError("source span mapping/segment membership mismatch")
        return value_id, doc_id, map_id, kind, segment_id, family

    def artifact(self, value_id: str) -> Artifact:
        rows = self.store._rows("SELECT * FROM te_artifacts WHERE id=?", (value_id,))
        if not rows:
            raise PersistenceError("missing text proposal/evidence artifact")
        row = rows[0]
        cls = next((t for k, t in ARTIFACT_TYPES if k == row[3]), None)
        if cls is None or not isinstance(row[1], str) or not isinstance(row[2], str):
            raise PersistenceError("invalid stored artifact kind")
        value = typed(row[-1], cls)
        segments = self.segments(row[1], row[2])
        if row[:-1] != self._artifact_scalars(value, row[1], row[2], segments):
            raise PersistenceError("text artifact scalar/membership mismatch")
        entities = (
            value.proposal.entity_ids
            if isinstance(value, ExtractedEventCandidate)
            else value.candidate_ids
            if isinstance(value, EntityMention)
            else ()
        )
        indexed = self.evidence._ids(
            "SELECT entity FROM te_entity_members WHERE artifact=? ORDER BY entity",
            (value_id,),
            10000,
        )
        if indexed != entities:
            raise PersistenceError("entity artifact index mismatch")
        return value

    def segments(self, document_id: str, map_id: str) -> tuple[TextSegment, ...]:
        ids = self.evidence._ids(
            "SELECT id FROM te_segments WHERE document=? AND map=? ORDER BY number,id",
            (document_id, map_id),
            4096,
        )
        return tuple(self.segment(i) for i in ids)

    @staticmethod
    def run_scalars(value: ExtractionRun) -> tuple[object, ...]:
        return (
            value.run_id,
            value.document_id,
            value.map_id,
            value.catalog_fingerprint,
            value.available_ns,
            None if value.model is None else value.model.model_id,
            None if value.model is None else value.model.model_version,
            value.template_fingerprint,
            value.status.value,
            value.result_fingerprint,
        )

    def optional_run(self, value_id: str) -> ExtractionRun | None:
        if not self.store._rows("SELECT id FROM te_runs WHERE id=?", (value_id,)):
            return None
        return self.run(value_id)

    def run(self, value_id: str) -> ExtractionRun:
        value = self._read("te_runs", value_id, ExtractionRun)
        self._read("te_runs", value_id, ExtractionRun, self.run_scalars(value))
        if value.run_id != value_id:
            raise PersistenceError("processing identity mismatch")
        self.document(value.document_id)
        self.mapping(value.map_id)
        self.catalog(value.catalog_fingerprint)
        if value.template_fingerprint is not None:
            self.template(value.template_fingerprint)
        for kind, ids in self.memberships(value):
            actual = self.evidence._ids(
                "SELECT artifact FROM te_members WHERE run=? AND kind=? ORDER BY artifact",
                (value_id, kind),
                10000,
            )
            if actual != ids:
                raise PersistenceError("incomplete or surplus run membership")
        if self.store._rows(
            "SELECT a.id FROM te_members m JOIN te_artifacts a ON m.artifact=a.id "
            "WHERE m.run=? AND (a.document<>? OR a.map<>? OR m.kind<>a.kind) LIMIT 1",
            (value_id, value.document_id, value.map_id),
        ):
            raise PersistenceError("run artifact document/map/kind mismatch")
        indexed = self.evidence._ids(
            "SELECT segment FROM te_run_segments WHERE run=? ORDER BY segment", (value_id,), 4096
        )
        if indexed != value.segment_ids:
            raise PersistenceError("run segment index mismatch")
        expected = tuple(
            sorted(
                (value_id, a.candidate_id, a.event_id, c, s)
                for a in value.accepted
                for s in a.assertion_ids
                for c in (self.evidence.assertion(s).claim_id,)
                if c in a.claim_ids
            )
        )
        actual_links = self.store._rows(
            "SELECT * FROM te_accepted WHERE run=? ORDER BY run,candidate,event,claim,assertion",
            (value_id,),
        )
        if actual_links != list(expected):
            raise PersistenceError("accepted claim index mismatch")
        for link in value.accepted:
            claims = {self.evidence.assertion(i).claim_id for i in link.assertion_ids}
            if claims != set(link.claim_ids) or any(
                self.evidence.assertion(i).event_id != link.event_id
                or self.evidence.assertion(i).known_from_ns != value.available_ns
                for i in link.assertion_ids
            ):
                raise PersistenceError("accepted assertion clock/event/claim mismatch")
        retracted = self.evidence._ids(
            "SELECT assertion FROM te_retractions WHERE run=? ORDER BY assertion",
            (value_id,),
            10000,
        )
        if retracted != value.retracted_assertion_ids:
            raise PersistenceError("retraction index mismatch")
        if value.receipt_id is not None:
            receipt = self.evidence.receipt(value.receipt_id)
            accepted_assertions = {i for link in value.accepted for i in link.assertion_ids}
            if (
                receipt is None
                or receipt.known_from_ns != value.available_ns
                or accepted_assertions | set(value.retracted_assertion_ids)
                != set(receipt.assertion_ids)
            ):
                raise PersistenceError("missing atomic extraction receipt")
            from eie.nlp.linkage import source_reference

            doc = self.document(value.document_id)
            mapping = self.mapping(value.map_id)
            expected_spans = []
            for sid in value.span_ids:
                span = self.artifact(sid)
                if not isinstance(span, EvidenceSpan):
                    raise PersistenceError("run span membership type mismatch")
                ref = source_reference(doc, mapping.original_characters, span)
                if ref.evidence_id in receipt.evidence_ids:
                    expected_spans.append((value_id, sid, ref.evidence_id))
            span_links = self.store._rows(
                "SELECT * FROM te_span_evidence WHERE run=? ORDER BY run,span,evidence", (value_id,)
            )
            if span_links != sorted(expected_spans):
                raise PersistenceError("source span/evidence link index mismatch")
        return value

    @staticmethod
    def memberships(run: ExtractionRun) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return (
            ("ENTITY", run.entity_mention_ids),
            ("NUMERIC", run.numeric_mention_ids),
            ("TEMPORAL", run.temporal_mention_ids),
            ("PERIOD", run.period_mention_ids),
            ("CANDIDATE", run.candidate_ids),
            ("VERIFICATION", run.verification_ids),
            ("SPAN", run.span_ids),
        )

    def append_inputs(self, artifacts: ExtractionArtifacts, payload: bytes) -> None:
        self.evidence.owned()
        p, run = artifacts.prepared, artifacts.run
        doc, mapping = p.document, p.normalized.mapping
        self.evidence.append_dataset(self.evidence.dataset(doc.dataset_id))
        self.evidence.validate_lineage(doc.source_lineage, doc.dataset_id)
        if doc.previous_document_id is not None:
            previous = self.document(doc.previous_document_id)
            if (
                previous.thread_id != doc.thread_id
                or previous.raw.source_item_version == doc.raw.source_item_version
                or previous.raw.ingested_at_ns > doc.raw.ingested_at_ns
            ):
                raise ValidationError(
                    "source revision requires same thread, new version and causal clocks"
                )
        if run.previous_run_id is not None:
            previous_run = self.run(run.previous_run_id)
            previous_doc = self.document(previous_run.document_id)
            if (
                previous_doc.thread_id != doc.thread_id
                or previous_run.available_ns >= run.available_ns
            ):
                raise ValidationError("reprocessing must advance the same source document thread")
        self.evidence._put("te_documents", self.document_scalars(doc), doc)
        if doc.retention.mode is RetentionMode.BOUNDED_RAW:
            if (
                doc.retention.permission is not RetentionPermission.ALLOWED
                or len(payload) > doc.retention.max_raw_bytes
            ):
                raise ValidationError("configured raw retention bound/entitlement exceeded")
            old = self.store._rows(
                "SELECT content FROM te_contents WHERE id=?", (doc.raw.payload_hash,)
            )
            if old and old != [(payload,)]:
                raise PersistenceError("content hash collision")
            if not old:
                self.store._connection.execute(
                    "INSERT INTO te_contents VALUES (?,?)", (doc.raw.payload_hash, payload)
                )
        self.evidence._put(
            "te_maps", (mapping.map_id, mapping.original_hash, mapping.normalized_hash), mapping
        )
        self.evidence._put("te_catalogs", (p.catalog.fingerprint,), p.catalog)
        if artifacts.template is not None:
            t = artifacts.template
            self.evidence._put("te_templates", (t.fingerprint, t.template_id, t.version), t)
        for s in p.segments:
            self.evidence._put(
                "te_segments", (s.segment_id, s.document_id, s.map_id, s.index, s.start, s.end), s
            )
        values: tuple[Artifact, ...] = (
            *p.entities,
            *p.numerics,
            *p.temporal,
            *p.periods,
            *artifacts.candidates,
            *artifacts.spans,
            *artifacts.verifications,
        )
        for value in values:
            scalars = self._artifact_scalars(value, doc.document_id, mapping.map_id, p.segments)
            if self.evidence._put("te_artifacts", scalars, value):
                entities = (
                    value.proposal.entity_ids
                    if isinstance(value, ExtractedEventCandidate)
                    else value.candidate_ids
                    if isinstance(value, EntityMention)
                    else ()
                )
                self.store._connection.executemany(
                    "INSERT INTO te_entity_members VALUES (?,?)",
                    ((scalars[0], i) for i in entities),
                )

    def append_run(self, run: ExtractionRun) -> None:
        self.evidence.owned()
        if self.evidence._put("te_runs", self.run_scalars(run), run):
            self.store._connection.executemany(
                "INSERT INTO te_members VALUES (?,?,?)",
                ((run.run_id, i, k) for k, ids in self.memberships(run) for i in ids),
            )
            self.store._connection.executemany(
                "INSERT INTO te_run_segments VALUES (?,?)",
                ((run.run_id, i) for i in run.segment_ids),
            )
            self.store._connection.executemany(
                "INSERT INTO te_accepted VALUES (?,?,?,?,?)",
                (
                    (run.run_id, a.candidate_id, a.event_id, self.evidence.assertion(s).claim_id, s)
                    for a in run.accepted
                    for s in a.assertion_ids
                ),
            )
            self.store._connection.executemany(
                "INSERT INTO te_retractions VALUES (?,?)",
                ((run.run_id, i) for i in run.retracted_assertion_ids),
            )
