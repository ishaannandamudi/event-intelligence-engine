"""Restart validation recalculates source grounding and complete accepted fact linkage."""

from eie.core.errors import EIEError, PersistenceError
from eie.evidence.schema import ClaimStatus
from eie.nlp.documents import ExtractionAcquisition
from eie.nlp.extraction import ExtractionArtifacts, prepare
from eie.nlp.linkage import grounded_claims, reference
from eie.nlp.models import ExtractionRun
from eie.nlp.prompts import ExtractionRequest, UntrustedSourceData
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.schema import SourceAction, VerificationState
from eie.nlp.spans import verify_span
from eie.nlp.validation_models import TextValidation, ValidationReason, ValidationStatus
from eie.nlp.verification import verify


def validate_run(repository: TextRepository, value: ExtractionRun) -> bool:
    """Returns whether exact body verification was possible; inconsistencies raise."""
    repo = repository
    doc = repo.document(value.document_id)
    if (
        value.available_ns < doc.raw.ingested_at_ns
        or repo.mapping(value.map_id).original_hash != doc.raw.payload_hash
    ):
        raise PersistenceError("processing availability or source hash mismatch")
    audit = TextQueries(repo, doc.dataset_id).audit(value.run_id, value.available_ns)
    if value.previous_run_id is not None:
        old = repo.run(value.previous_run_id)
        if (
            repo.document(old.document_id).thread_id != doc.thread_id
            or old.available_ns >= value.available_ns
        ):
            raise PersistenceError("noncausal extraction history")
    if len(audit.verifications) != len(audit.candidates) or {
        v.candidate_id for v in audit.verifications
    } != set(value.candidate_ids):
        raise PersistenceError("incomplete candidate verification")
    verified = {
        v.candidate_id: v for v in audit.verifications if v.state is VerificationState.VERIFIED
    }
    for link in value.accepted:
        if link.candidate_id not in verified:
            raise PersistenceError("unverified candidate escaped acceptance gate")
        candidate = next(c for c in audit.candidates if c.candidate_id == link.candidate_id)
        event = repo.store.get_event(link.event_id, value.available_ns)
        if event is None or event.identity.scope.identifiers != candidate.proposal.entity_ids:
            raise PersistenceError("accepted event identity mismatch")
        facts = grounded_claims(candidate, link.event_id, event.identity.event_type)
        expected = {claim.claim_id: result for _, claim, result in facts}
        if set(expected) != set(link.claim_ids):
            raise PersistenceError("accepted candidate fact slots mismatch")
        for i in link.assertion_ids:
            assertion = repo.evidence.assertion(i)
            lineage = repo.evidence.lineage(assertion.lineage_id)
            if (
                assertion.value != expected.get(assertion.claim_id)
                or not isinstance(lineage, ExtractionAcquisition)
                or lineage.processing_fingerprint != value.processing_fingerprint
                or lineage.extraction_available_ns != value.available_ns
            ):
                raise PersistenceError("unsupported accepted value or processing lineage")
    if value.retracted_assertion_ids:
        if (
            doc.action is not SourceAction.RETRACTED
            or value.accepted
            or any(
                repo.evidence.assertion(i).status is not ClaimStatus.RETRACTED
                for i in value.retracted_assertion_ids
            )
        ):
            raise PersistenceError("invalid source withdrawal semantics")
    payload = repo.content(doc.document_id)
    if payload is None:
        return False
    p = prepare(
        doc, payload, repo.catalog(value.catalog_fingerprint), value.available_ns, value.settings
    )
    if (
        p.normalized.mapping != audit.mapping
        or tuple(sorted(s.segment_id for s in p.segments)) != value.segment_ids
    ):
        raise PersistenceError("normalization/segmentation recalculation mismatch")
    expected_mentions = (
        tuple(sorted(m.mention_id for m in p.entities)),
        tuple(sorted(m.mention_id for m in p.numerics)),
        tuple(sorted(m.mention_id for m in p.temporal)),
        tuple(sorted(m.mention_id for m in p.periods)),
    )
    if expected_mentions != (
        value.entity_mention_ids,
        value.numeric_mention_ids,
        value.temporal_mention_ids,
        value.period_mention_ids,
    ):
        raise PersistenceError("deterministic mention recalculation mismatch")
    for span in audit.spans:
        verify_span(span, p.original, p.normalized, p.segments)
    for candidate in audit.candidates:
        current, _ = verify(p, candidate)
        stored = next(v for v in audit.verifications if v.candidate_id == candidate.candidate_id)
        if current != stored:
            raise PersistenceError("source verification recalculation mismatch")
    if value.model is not None and value.template_fingerprint is not None:
        request = ExtractionRequest(
            repo.template(value.template_fingerprint),
            value.model,
            UntrustedSourceData(
                doc.document_id,
                p.normalized.mapping.normalized_hash,
                p.normalized.text,
                tuple((s.segment_id, s.start, s.end) for s in p.segments),
            ),
        )
        if request.fingerprint != value.request_fingerprint:
            raise PersistenceError("model request fingerprint mismatch")
    artifacts = ExtractionArtifacts(
        p, audit.candidates, audit.verifications, audit.spans, value, None
    )
    expected_refs = {reference(artifacts, s).evidence_id for s in audit.spans}
    if any(r.evidence_id not in expected_refs for r in audit.evidence):
        raise PersistenceError("accepted reference lacks exact original document span")
    return True


def validate_store(repository: TextRepository, *, max_records: int = 100000) -> TextValidation:
    repo = repository
    counts: list[tuple[str, int]] = []
    warnings = False
    with repo.store.transaction():
        try:
            if repo.store._rows("PRAGMA foreign_key_check"):
                raise PersistenceError("text foreign reference integrity failure")
            for table, read in (
                ("te_documents", repo.document),
                ("te_maps", repo.mapping),
                ("te_segments", repo.segment),
                ("te_catalogs", repo.catalog),
                ("te_templates", repo.template),
                ("te_artifacts", repo.artifact),
                ("te_runs", repo.run),
            ):
                ids = repo.evidence._ids(f"SELECT id FROM {table} ORDER BY id", (), max_records)
                for i in ids:
                    read(i)
                counts.append((table, len(ids)))
            for i in repo.evidence._ids(
                "SELECT id FROM te_runs ORDER BY known,id", (), max_records
            ):
                warnings |= not validate_run(repo, repo.run(i))
            for content_id, content in repo.store._rows(
                "SELECT id,content FROM te_contents ORDER BY id"
            ):
                from hashlib import sha256

                if not isinstance(content, bytes) or sha256(content).hexdigest() != content_id:
                    raise PersistenceError("retained source content corruption")
            return TextValidation(
                ValidationStatus.VALID_WITH_WARNINGS if warnings else ValidationStatus.VALID,
                (ValidationReason.CONTENT_NOT_RETAINED,) if warnings else (),
                tuple(sorted(counts)),
            )
        except EIEError:
            return TextValidation(
                ValidationStatus.INVALID,
                (ValidationReason.INTEGRITY_FAILURE,),
                tuple(sorted(counts)),
            )
