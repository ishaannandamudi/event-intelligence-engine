"""Bounded deterministic first pass and optional, untrusted model proposals."""

import re
from dataclasses import dataclass, field
from hashlib import sha256

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.entities.protocols import ResolutionState
from eie.evidence.claims import TimePrecision
from eie.nlp.classification import classify, epistemic
from eie.nlp.documents import TextDocument
from eie.nlp.entities import EntityCatalog, extract_entities
from eie.nlp.mentions import EntityMention, NumericMention, PeriodMention, TemporalMention
from eie.nlp.model_protocol import StructuredExtractionModel
from eie.nlp.models import (
    EventProposal,
    ExtractedEventCandidate,
    ExtractionRun,
    ExtractionSettings,
    NumericFieldProposal,
    PeriodProposal,
    SpanProposal,
    TemporalFieldProposal,
    TextFieldProposal,
    VerificationResult,
)
from eie.nlp.normalization import NormalizedText, normalize
from eie.nlp.numerics import extract_numerics
from eie.nlp.prompts import ExtractionRequest, PromptTemplate, UntrustedSourceData
from eie.nlp.schema import (
    ExtractionFailure,
    ExtractionMethod,
    ExtractionQuality,
    FieldName,
    RunStatus,
    SourceAction,
    UnscheduledFamily,
    VerificationState,
)
from eie.nlp.security import OutputSchemaError, decode_proposals
from eie.nlp.segmentation import TextSegment, segment
from eie.nlp.spans import EvidenceSpan
from eie.nlp.temporal import event_time_supported, extract_periods, extract_temporal

DEFAULT_SETTINGS = ExtractionSettings()
DEFAULT_TEMPLATE = PromptTemplate()


@dataclass(frozen=True, slots=True)
class PreparedText:
    document: TextDocument
    original: str = field(repr=False)
    normalized: NormalizedText
    catalog: EntityCatalog
    segments: tuple[TextSegment, ...]
    entities: tuple[EntityMention, ...]
    numerics: tuple[NumericMention, ...]
    temporal: tuple[TemporalMention, ...]
    periods: tuple[PeriodMention, ...]


@dataclass(frozen=True, slots=True)
class ExtractionArtifacts:
    prepared: PreparedText
    candidates: tuple[ExtractedEventCandidate, ...]
    verifications: tuple[VerificationResult, ...]
    spans: tuple[EvidenceSpan, ...]
    run: ExtractionRun
    template: PromptTemplate | None


def proposed_span(span: EvidenceSpan) -> SpanProposal:
    return SpanProposal(span.start, span.end, span.normalized_fragment_hash)


def prepare(
    document: TextDocument,
    payload: bytes,
    catalog: EntityCatalog,
    available_ns: int,
    settings: ExtractionSettings = DEFAULT_SETTINGS,
) -> PreparedText:
    require_ns(available_ns)
    if (
        available_ns < document.raw.ingested_at_ns
        or sha256(payload).hexdigest() != document.raw.payload_hash
    ):
        raise ValidationError("source hash or extraction availability disagrees")
    normalized = normalize(payload, document.format, settings.normalization)
    original = payload.decode("utf-8")
    segments = segment(document.document_id, normalized)
    supported = document.language.lower() in ("en", "en-us", "en-gb")
    result = PreparedText(
        document,
        original,
        normalized,
        catalog,
        segments,
        extract_entities(
            original,
            normalized,
            segments,
            catalog,
            available_ns,
            document.raw.published_at_ns or document.raw.source_observed_at_ns,
        )
        if supported
        else (),
        extract_numerics(original, normalized, segments, document.currency) if supported else (),
        extract_temporal(original, normalized, segments, document) if supported else (),
        extract_periods(original, normalized, segments) if supported else (),
    )
    if (
        sum(
            len(items)
            for items in (result.entities, result.numerics, result.temporal, result.periods)
        )
        > settings.normalization.limits.mentions
    ):
        raise ValidationError("total document mention bound exceeded")
    return result


def text_fields(
    prepared: PreparedText,
    seg: TextSegment,
    family: UnscheduledFamily,
    *,
    start: int | None = None,
    end: int | None = None,
) -> tuple[TextFieldProposal, ...]:
    """Small explicit patterns; literal values must survive the same verifier as model output."""
    offset = seg.start if start is None else start
    text = prepared.normalized.text[offset : seg.end if end is None else end]
    patterns: list[tuple[FieldName, str]] = []
    if family is UnscheduledFamily.MANAGEMENT_CHANGE:
        patterns.extend(
            [
                (FieldName.ROLE, r"\b(?:CEO|CFO|director|auditor)\b"),
                (
                    FieldName.PERSON,
                    r"\b(?:CEO|CFO|director|auditor)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b",
                ),
                (FieldName.ACTION, r"\b(?:resigned|departed|appointed|resigns|appoints)\b"),
            ]
        )
    if family in (UnscheduledFamily.LEGAL_REGULATORY, UnscheduledFamily.FDA_REGULATORY_DECISION):
        patterns.extend(
            [
                (FieldName.AGENCY, r"\b(?:FDA|SEC|FTC|DOJ|Court of Appeals)\b"),
                (
                    FieldName.ACTION,
                    r"\b(?:approved|rejected|investigation|lawsuit|regulatory fine|court order)\b",
                ),
            ]
        )
    if family is UnscheduledFamily.M_AND_A:
        patterns.extend(
            [
                (FieldName.ACQUIRER, r"\bacquirer\s+(CIK\s*[:#]?\s*\d{1,10})\b"),
                (FieldName.TARGET, r"\btarget\s+(CIK\s*[:#]?\s*\d{1,10})\b"),
                (FieldName.ACTION, r"\b(?:acquired|acquisition|merger|announced)\b"),
            ]
        )
    if family is UnscheduledFamily.CYBERSECURITY:
        patterns.append(
            (FieldName.IMPACT, r"\b(?:operations halted|production stopped|systems offline)\b")
        )
    result = []
    for name, pattern in patterns:
        matches = tuple(re.finditer(pattern, text, re.I if name is not FieldName.PERSON else 0))
        if len(matches) != 1:
            continue
        m = matches[0]
        a, b = m.span(1) if m.lastindex else m.span()
        value = text[a:b]
        result.append(
            TextFieldProposal(
                name,
                value,
                SpanProposal(offset + a, offset + b, sha256(value.encode()).hexdigest()),
            )
        )
    return tuple(sorted(result, key=lambda f: f.name))


def management_contexts(text: str) -> tuple[tuple[int, int], ...]:
    boundaries = [0]
    boundaries.extend(
        m.end()
        for m in re.finditer(
            r";\s*|\.\s+|(?:,\s*(?:and\s+)?|\band\s+)"
            r"(?=(?:CEO|CFO|director|auditor)\b)",
            text,
            re.I,
        )
    )
    boundaries.append(len(text))
    return tuple(zip(boundaries, boundaries[1:], strict=False))


def management_proposals(
    prepared: PreparedText, seg: TextSegment, entities: tuple[str, ...]
) -> tuple[EventProposal, ...]:
    """Separate explicit clauses, retaining the full segment as security context.

    No person, role, action or effective time is inherited from another clause.
    The parent segment's exact issuer applicability and conservative epistemic
    status remain shared. Ambiguous undelimited statements remain unverified.
    """
    text = prepared.normalized.text[seg.start : seg.end]
    result = []
    family = UnscheduledFamily.MANAGEMENT_CHANGE
    parent_status = epistemic(text, family)
    for index, (a, b) in enumerate(management_contexts(text)):
        local = text[a:b]
        if family not in classify(local):
            continue
        times = tuple(
            m
            for m in prepared.temporal
            if seg.start + a <= m.span.start < m.span.end <= seg.start + b
            and m.value.precision is not TimePrecision.UNKNOWN
            and event_time_supported(prepared.normalized, m.span, prepared.segments)
        )
        result.append(
            EventProposal(
                f"management_change:{seg.index}:{index}",
                family,
                parent_status,
                SpanProposal(seg.start, seg.end, seg.content_hash),
                entities,
                text_fields=text_fields(
                    prepared, seg, family, start=seg.start + a, end=seg.start + b
                ),
                temporal=TemporalFieldProposal(times[0].value, proposed_span(times[0].span))
                if len(times) == 1
                else None,
            )
        )
    return tuple(result)


def management_source(prepared: PreparedText, proposal: EventProposal) -> EventProposal | None:
    """Match a proposal to one deterministic source clause, never its model key."""
    seg = next(
        s
        for s in prepared.segments
        if s.start == proposal.span.start and s.end == proposal.span.end
    )
    sources = management_proposals(prepared, seg, proposal.entity_ids)
    matches = tuple(
        p
        for p in sources
        if p.text_fields == proposal.text_fields and p.temporal == proposal.temporal
    )
    return matches[0] if len(matches) == 1 else None


def deterministic(prepared: PreparedText) -> tuple[EventProposal, ...]:
    result: list[EventProposal] = []
    for seg in prepared.segments:
        text = prepared.normalized.text[seg.start : seg.end]
        entities = tuple(
            sorted(
                {
                    i
                    for m in prepared.entities
                    if m.span.segment_id == seg.segment_id and m.state is ResolutionState.RESOLVED
                    for i in m.candidate_ids
                }
            )
        )
        for family in classify(text):
            if family is UnscheduledFamily.MANAGEMENT_CHANGE:
                result.extend(management_proposals(prepared, seg, entities))
                continue
            by_name: dict[FieldName, list[NumericMention]] = {}
            for m in prepared.numerics:
                if m.span.segment_id != seg.segment_id:
                    continue
                if family is UnscheduledFamily.EARNINGS_RESULT and m.field not in (
                    FieldName.REVENUE,
                    FieldName.EPS,
                    FieldName.MARGIN,
                ):
                    continue
                if family is UnscheduledFamily.GUIDANCE_CHANGE and m.field not in (
                    FieldName.GUIDANCE_REVENUE,
                    FieldName.GUIDANCE_EPS,
                    FieldName.MARGIN,
                ):
                    continue
                if (
                    family
                    not in (UnscheduledFamily.EARNINGS_RESULT, UnscheduledFamily.GUIDANCE_CHANGE)
                    and m.field is not FieldName.AMOUNT
                ):
                    continue
                by_name.setdefault(m.field, []).append(m)
            fields = tuple(
                NumericFieldProposal(
                    name,
                    items[0].quantity,
                    proposed_span(items[0].span),
                    items[0].upper,
                    items[0].bound,
                    items[0].qualifiers,
                )
                for name, items in sorted(by_name.items())
                if len(items) == 1
            )
            periods = tuple(m for m in prepared.periods if m.span.segment_id == seg.segment_id)
            times = tuple(
                m
                for m in prepared.temporal
                if m.span.segment_id == seg.segment_id
                and m.value.precision is not TimePrecision.UNKNOWN
                and event_time_supported(prepared.normalized, m.span, prepared.segments)
                and not any(p.span.start <= m.span.start < p.span.end for p in periods)
            )
            result.append(
                EventProposal(
                    f"{family.value.lower()}:{seg.index}",
                    family,
                    epistemic(text, family),
                    SpanProposal(seg.start, seg.end, seg.content_hash),
                    entities,
                    fields,
                    text_fields(prepared, seg, family),
                    TemporalFieldProposal(times[0].value, proposed_span(times[0].span))
                    if len(times) == 1
                    else None,
                    PeriodProposal(periods[0].period, proposed_span(periods[0].span))
                    if len(periods) == 1
                    else None,
                )
            )
    if len(result) > prepared.normalized.mapping.settings.limits.candidates:
        raise ValidationError("deterministic candidate bound exceeded")
    return tuple(sorted(result, key=lambda p: p.key))


def extract(
    document: TextDocument,
    payload: bytes,
    catalog: EntityCatalog,
    available_ns: int,
    *,
    settings: ExtractionSettings = DEFAULT_SETTINGS,
    model: StructuredExtractionModel | None = None,
    template: PromptTemplate = DEFAULT_TEMPLATE,
    previous_run_id: str | None = None,
) -> ExtractionArtifacts:
    from eie.nlp.verification import verify

    prepared = prepare(document, payload, catalog, available_ns, settings)
    candidates: list[ExtractedEventCandidate] = []
    failures: set[ExtractionFailure] = set()
    request, output_hash = None, None
    supported = document.language.lower() in ("en", "en-us", "en-gb")
    if not supported:
        failures.add(ExtractionFailure.UNSUPPORTED_LANGUAGE)
    elif document.action is not SourceAction.RETRACTED:
        candidates.extend(
            ExtractedEventCandidate(
                document.document_id,
                p,
                ExtractionMethod.DETERMINISTIC,
                ExtractionQuality.EXACT_PATTERN,
            )
            for p in deterministic(prepared)
        )
    if model is not None:
        request = ExtractionRequest(
            template,
            model.descriptor,
            UntrustedSourceData(
                document.document_id,
                prepared.normalized.mapping.normalized_hash,
                prepared.normalized.text,
                tuple((s.segment_id, s.start, s.end) for s in prepared.segments),
            ),
        )
        if supported and document.action is not SourceAction.RETRACTED:
            try:
                output = model.extract(request)
                if not isinstance(output, bytes):
                    failures.add(ExtractionFailure.INVALID_OUTPUT)
                else:
                    if len(output) > settings.normalization.limits.model_output_bytes:
                        raise OutputSchemaError("model output byte bound exceeded")
                    output_hash = sha256(output).hexdigest()
                    proposals = decode_proposals(output, settings.normalization.limits)
                    candidates.extend(
                        ExtractedEventCandidate(
                            document.document_id,
                            p,
                            ExtractionMethod.MODEL_ASSISTED,
                            ExtractionQuality.MODEL_PROPOSAL,
                        )
                        for p in proposals
                    )
            except TimeoutError:
                failures.add(ExtractionFailure.MODEL_TIMEOUT)
            except (ConnectionError, OSError):
                failures.add(ExtractionFailure.MODEL_UNAVAILABLE)
            except OutputSchemaError:
                failures.add(ExtractionFailure.SCHEMA_FAILURE)
            except Exception:
                # Adapter failures expose only a fixed category, never raw exception/source text.
                failures.add(ExtractionFailure.INVALID_OUTPUT)
    candidates.sort(key=lambda c: c.candidate_id)
    if len(candidates) > settings.normalization.limits.candidates:
        raise ValidationError("combined candidate bound exceeded")
    verifications: list[VerificationResult] = []
    spans: dict[str, EvidenceSpan] = {}
    for candidate in candidates:
        result, evidence = verify(prepared, candidate)
        verifications.append(result)
        spans.update((s.span_id, s) for s in evidence)
        if result.state is not VerificationState.VERIFIED:
            failures.add(ExtractionFailure.VERIFICATION_FAILURE)
    # Mentions remain independently auditable, including unresolved/rejected proposals.
    mentions: tuple[EntityMention | NumericMention | TemporalMention | PeriodMention, ...] = (
        *prepared.entities,
        *prepared.numerics,
        *prepared.temporal,
        *prepared.periods,
    )
    for mention in mentions:
        spans[mention.span.span_id] = mention.span
    if len(spans) > settings.normalization.limits.spans:
        raise ValidationError("extracted span bound exceeded")
    if not candidates and not failures and document.action is not SourceAction.RETRACTED:
        failures.add(ExtractionFailure.NO_EVENT_EXTRACTED)
    verified = any(v.state is VerificationState.VERIFIED for v in verifications)
    status = (
        RunStatus.PARTIAL
        if failures and verified
        else RunStatus.FAILED
        if failures
        else RunStatus.SUCCESS
    )
    if candidates and not verified:
        status = RunStatus.REJECTED
    run = ExtractionRun(
        document.document_id,
        prepared.normalized.mapping.map_id,
        settings,
        catalog.fingerprint,
        available_ns,
        None if request is None else request.model,
        None if request is None else template.fingerprint,
        None if request is None else request.fingerprint,
        output_hash,
        tuple(sorted(s.segment_id for s in prepared.segments)),
        tuple(sorted(m.mention_id for m in prepared.entities)),
        tuple(sorted(m.mention_id for m in prepared.numerics)),
        tuple(sorted(m.mention_id for m in prepared.temporal)),
        tuple(sorted(m.mention_id for m in prepared.periods)),
        tuple(c.candidate_id for c in candidates),
        tuple(sorted(v.verification_id for v in verifications)),
        tuple(sorted(spans)),
        (),
        None,
        status,
        tuple(sorted(failures)),
        previous_run_id,
    )
    return ExtractionArtifacts(
        prepared,
        tuple(candidates),
        tuple(sorted(verifications, key=lambda v: v.verification_id)),
        tuple(spans[k] for k in sorted(spans)),
        run,
        template if request else None,
    )
