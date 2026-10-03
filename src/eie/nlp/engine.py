"""Completed text extraction and accepted EIE-11 claims commit as one source item."""

import re
from dataclasses import replace

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.engine import EvidenceEngine
from eie.evidence.schema import ClaimStatus
from eie.nlp.documents import TextDocument
from eie.nlp.entities import EntityCatalog
from eie.nlp.extraction import DEFAULT_TEMPLATE, ExtractionArtifacts, extract
from eie.nlp.linkage import acquisition, batch, reference
from eie.nlp.model_protocol import StructuredExtractionModel
from eie.nlp.models import AcceptedCandidateLink, ExtractionRun
from eie.nlp.prompts import PromptTemplate
from eie.nlp.repository import TextRepository
from eie.nlp.schema import SourceAction
from eie.nlp.security import instruction_like
from eie.nlp.spans import make_span
from eie.providers.evidence.models import ClaimInput, EvidenceBatch
from eie.providers.text.models import TextProvider


class TextEngine:
    def __init__(self, repository: TextRepository) -> None:
        self.repository = repository
        self.evidence_engine = EvidenceEngine(repository.evidence)

    def _retraction(
        self, artifacts: ExtractionArtifacts
    ) -> tuple[ExtractionArtifacts, EvidenceBatch]:
        p, doc = artifacts.prepared, artifacts.prepared.document
        repo = self.repository
        if doc.previous_document_id is None:
            raise ValidationError("source withdrawal requires a prior source document")
        previous = repo.document(doc.previous_document_id)
        if previous.thread_id != doc.thread_id:
            raise ValidationError("source withdrawal cannot cross document threads")
        spans = tuple(
            make_span(p.original, p.normalized, s, s.start, s.end)
            for s in p.segments
            if re.search(
                r"\b(?:retracted|retracts|withdrawn|withdraws)\b",
                p.normalized.text[s.start : s.end],
                re.I,
            )
            and not instruction_like(p.normalized.text[s.start : s.end])
        )
        if not spans:
            raise ValidationError("source withdrawal needs explicit inert withdrawal evidence")
        all_spans = {s.span_id: s for s in (*artifacts.spans, *spans)}
        artifacts = replace(
            artifacts,
            spans=tuple(all_spans[i] for i in sorted(all_spans)),
            run=replace(artifacts.run, span_ids=tuple(sorted(all_spans))),
        )
        if repo.optional_run(artifacts.run.run_id) is not None:
            return artifacts, EvidenceBatch(
                doc.resource_id, doc.raw, acquisition(artifacts), (), ()
            )
        ids = repo.evidence._ids(
            "SELECT DISTINCT a.assertion FROM te_accepted a JOIN te_runs r ON a.run=r.id "
            "JOIN te_documents d ON r.document=d.id WHERE d.thread=? "
            "AND r.known<? ORDER BY a.assertion",
            (doc.thread_id, artifacts.run.available_ns),
            artifacts.run.settings.normalization.limits.accepted_claims,
        )
        latest = {}
        for i in ids:
            old = repo.evidence.assertion(i)
            current = repo.evidence.latest_thread(old.thread_id)
            if current is not None and current.status not in (
                ClaimStatus.RETRACTED,
                ClaimStatus.WITHDRAWN,
            ):
                latest[current.thread_id] = current
        if not latest:
            raise ValidationError("source withdrawal requires active accepted source claims")
        refs = tuple(sorted((reference(artifacts, s) for s in spans), key=lambda r: r.evidence_id))
        claims = tuple(
            sorted(
                (
                    ClaimInput(
                        a.provider_claim_key,
                        repo.evidence.claim(a.claim_id),
                        a.value,
                        tuple(r.evidence_id for r in refs),
                        ClaimStatus.RETRACTED,
                        event_id=a.event_id,
                    )
                    for a in latest.values()
                ),
                key=lambda c: c.provider_claim_key,
            )
        )
        return artifacts, EvidenceBatch(
            doc.resource_id, doc.raw, acquisition(artifacts), refs, claims
        )

    def ingest(
        self,
        provider: TextProvider,
        document: TextDocument,
        payload: bytes,
        catalog: EntityCatalog,
        available_ns: int,
        *,
        model: StructuredExtractionModel | None = None,
        template: PromptTemplate = DEFAULT_TEMPLATE,
        previous_run_id: str | None = None,
    ) -> ExtractionRun:
        from eie.ingestion.models import RetentionPermission

        conf = provider.evidence
        if (
            document.raw.source != conf.source
            or document.source_family != conf.source_family
            or document.dataset_id != conf.dataset.dataset_id
            or document.retention != conf.archive_policy
            or document.timezone != provider.timezone
            or document.currency != provider.currency
            or document.source_lineage.parser_version != conf.parser_version
            or document.source_lineage.build_id != conf.dataset.build_id
            or conf.normalized_retention is not RetentionPermission.ALLOWED
        ):
            raise ValidationError(
                "text document disagrees with trusted source/retention configuration"
            )
        artifacts = extract(
            document,
            payload,
            catalog,
            available_ns,
            settings=provider.settings,
            model=model,
            template=template,
            previous_run_id=previous_run_id,
        )
        repo, evidence = self.repository, self.repository.evidence
        with repo.store.transaction():
            if document.action is SourceAction.RETRACTED:
                artifacts, item = self._retraction(artifacts)
                links: dict[str, tuple[str, tuple[str, ...]]] = {}
            else:
                # An existing exact run returns before generating a new source event revision.
                existing = repo.optional_run(artifacts.run.run_id)
                if existing is not None:
                    return existing
                item, links = batch(evidence, artifacts)
            existing = repo.optional_run(artifacts.run.run_id)
            if existing is not None:
                return existing
            clocks = repo.store._rows(
                "SELECT MAX(r.known) FROM te_runs r JOIN te_documents d ON r.document=d.id "
                "WHERE d.dataset=?",
                (document.dataset_id,),
            )
            if clocks and isinstance(clocks[0][0], int) and available_ns < clocks[0][0]:
                raise ValidationError("completed extraction availability cannot be backdated")
            evidence.knowledge_append(document.dataset_id, available_ns)
            evidence.append_dataset(conf.dataset)
            receipt = None
            if item.claims:
                receipt = self.evidence_engine.ingest(conf, item, source_payload=payload)
            else:
                old = repo.store.get_observation(document.raw.observation_id)
                if old is None:
                    repo.store.append_observation(document.raw)
                elif old != document.raw:
                    raise PersistenceError("source metadata changed without a new version")
            evidence.validate_lineage(document.source_lineage, document.dataset_id)
            accepted = []
            if receipt is not None:
                assertions = tuple(evidence.assertion(i) for i in receipt.assertion_ids)
                for cid, (event_id, claim_ids) in sorted(links.items()):
                    accepted.append(
                        AcceptedCandidateLink(
                            cid,
                            event_id,
                            claim_ids,
                            tuple(
                                sorted(
                                    a.observation_id
                                    for a in assertions
                                    if a.event_id == event_id and a.claim_id in claim_ids
                                )
                            ),
                        )
                    )
            run = replace(
                artifacts.run,
                accepted=tuple(accepted),
                receipt_id=None if receipt is None else receipt.receipt_id,
                retracted_assertion_ids=()
                if receipt is None or document.action is not SourceAction.RETRACTED
                else receipt.assertion_ids,
            )
            artifacts = replace(artifacts, run=run)
            repo.append_inputs(artifacts, payload)
            repo.append_run(run)
            if receipt is not None:
                for span in artifacts.spans:
                    ref = reference(artifacts, span)
                    if ref.evidence_id in receipt.evidence_ids:
                        repo.store._connection.execute(
                            "INSERT INTO te_span_evidence VALUES (?,?,?)",
                            (run.run_id, span.span_id, ref.evidence_id),
                        )
            return repo.run(run.run_id)
