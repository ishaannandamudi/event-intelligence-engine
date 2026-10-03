"""Models cannot upgrade source facts, identity, precision, units or epistemic status."""

import re

from eie.core.errors import ValidationError
from eie.entities.protocols import ResolutionState
from eie.expectations.units import Quantity
from eie.nlp.classification import PATTERNS, classify, epistemic
from eie.nlp.extraction import (
    PreparedText,
    management_contexts,
    management_source,
    proposed_span,
    text_fields,
)
from eie.nlp.models import ExtractedEventCandidate, SpanProposal, VerificationResult
from eie.nlp.schema import (
    EpistemicStatus,
    FieldName,
    NumericBound,
    UnscheduledFamily,
    VerificationReason,
    VerificationState,
)
from eie.nlp.security import instruction_like
from eie.nlp.spans import EvidenceSpan, make_span
from eie.nlp.temporal import event_time_supported


def equal_quantity(left: Quantity | None, right: Quantity | None) -> bool:
    return (left is None and right is None) or (
        left is not None
        and right is not None
        and left.canonical_unit == right.canonical_unit
        and left.canonical_value == right.canonical_value
    )


def verify(
    prepared: PreparedText, candidate: ExtractedEventCandidate
) -> tuple[VerificationResult, tuple[EvidenceSpan, ...]]:
    reasons: set[VerificationReason] = set()
    spans: dict[str, EvidenceSpan] = {}
    proposal = candidate.proposal
    segments = tuple(
        s
        for s in prepared.segments
        if s.start == proposal.span.start and s.end == proposal.span.end
    )
    if candidate.document_id != prepared.document.document_id or len(segments) != 1:
        return VerificationResult(
            candidate.candidate_id,
            VerificationState.REJECTED,
            (VerificationReason.SPAN_INVALID,),
            (),
        ), ()
    seg = segments[0]
    text = prepared.normalized.text[seg.start : seg.end]

    def grounded(value: SpanProposal) -> bool:
        try:
            span = make_span(prepared.original, prepared.normalized, seg, value.start, value.end)
            if span.normalized_fragment_hash != value.fragment_hash:
                raise ValidationError("proposed fragment hash disagrees")
            spans[span.span_id] = span
            return True
        except ValidationError:
            reasons.add(VerificationReason.SPAN_INVALID)
            return False

    grounded(proposal.span)
    if instruction_like(text) or proposal.family not in classify(text):
        reasons.add(VerificationReason.CLASSIFICATION_UNSUPPORTED)
    pattern = next(p for family, p in PATTERNS if family is proposal.family)
    management = (
        management_source(prepared, proposal)
        if proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE
        else None
    )
    multiple = len(tuple(re.finditer(pattern, text, re.I))) > 1
    if multiple and (
        management is None
        or {f.name for f in management.text_fields}
        != {FieldName.ROLE, FieldName.PERSON, FieldName.ACTION}
    ):
        reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
    if proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE and management is None:
        reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
    if management is not None:
        a, b = management_contexts(text)[int(management.key.rsplit(":", 1)[1])]
        times = tuple(
            m
            for m in prepared.temporal
            if seg.start + a <= m.span.start < m.span.end <= seg.start + b
            and event_time_supported(prepared.normalized, m.span, prepared.segments)
        )
        if {f.name for f in management.text_fields} != {
            FieldName.ROLE,
            FieldName.PERSON,
            FieldName.ACTION,
        } or len(times) > 1:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
    actual_status = epistemic(text, proposal.family)
    if proposal.epistemic is not actual_status:
        reasons.add(VerificationReason.NEGATION_CONFLICT)
    if actual_status is not EpistemicStatus.REPORTED:
        reasons.add(VerificationReason.NON_FACTUAL_STATUS)
    entity_mentions = tuple(
        m
        for m in prepared.entities
        if m.span.segment_id == seg.segment_id and m.state is ResolutionState.RESOLVED
    )
    resolved = tuple(sorted({i for m in entity_mentions for i in m.candidate_ids}))
    if not resolved or not proposal.entity_ids:
        reasons.add(VerificationReason.ENTITY_UNRESOLVED)
    elif proposal.entity_ids != resolved:
        reasons.add(VerificationReason.ENTITY_MISMATCH)
    if proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE and len(resolved) > 1:
        reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
    for m in entity_mentions:
        spans[m.span.span_id] = m.span
    for field in proposal.numerics:
        grounded(field.span)
        matches = tuple(m for m in prepared.numerics if proposed_span(m.span) == field.span)
        if len(matches) != 1:
            reasons.add(VerificationReason.NUMERIC_MISMATCH)
            continue
        mention = matches[0]
        if mention.ambiguous:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if (
            sum(
                m.span.segment_id == seg.segment_id
                and m.field is field.name
                and m.qualifiers == field.qualifiers
                for m in prepared.numerics
            )
            != 1
        ):
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if mention.quantity.canonical_unit != field.quantity.canonical_unit:
            reasons.add(VerificationReason.UNIT_MISMATCH)
        if (
            not equal_quantity(mention.quantity, field.quantity)
            or not equal_quantity(mention.upper, field.upper)
            or mention.bound is not field.bound
        ):
            reasons.add(VerificationReason.NUMERIC_MISMATCH)
        if mention.field is not field.name:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if mention.qualifiers != field.qualifiers:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if field.bound in (NumericBound.MORE_THAN, NumericBound.LESS_THAN):
            # EIE-11 ranges have inclusive bounds; an exclusive bound cannot be widened.
            reasons.add(VerificationReason.FIELD_UNSUPPORTED)
        if len(resolved) != 1:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if field.name is not FieldName.AMOUNT and proposal.period is None:
            reasons.add(VerificationReason.REFERENCE_PERIOD_MISMATCH)
        allowed = (
            (FieldName.REVENUE, FieldName.EPS, FieldName.MARGIN)
            if proposal.family is UnscheduledFamily.EARNINGS_RESULT
            else (FieldName.GUIDANCE_REVENUE, FieldName.GUIDANCE_EPS, FieldName.MARGIN)
            if proposal.family is UnscheduledFamily.GUIDANCE_CHANGE
            else (FieldName.AMOUNT,)
        )
        if field.name not in allowed:
            reasons.add(VerificationReason.FIELD_UNSUPPORTED)
    expected_text = (
        management.text_fields
        if management is not None
        else text_fields(prepared, seg, proposal.family)
    )
    for text_field in proposal.text_fields:
        grounded(text_field.span)
        if text_field not in expected_text:
            reasons.add(VerificationReason.FIELD_UNSUPPORTED)
    if proposal.temporal is not None:
        grounded(proposal.temporal.span)
        if not any(
            proposed_span(m.span) == proposal.temporal.span and m.value == proposal.temporal.value
            for m in prepared.temporal
        ):
            reasons.add(VerificationReason.TIME_UNSUPPORTED)
        matching = next(
            (m for m in prepared.temporal if proposed_span(m.span) == proposal.temporal.span), None
        )
        if matching is not None and (
            not event_time_supported(prepared.normalized, matching.span, prepared.segments)
            or any(p.span.start <= matching.span.start < p.span.end for p in prepared.periods)
        ):
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
    if proposal.period is not None:
        grounded(proposal.period.span)
        if sum(m.span.segment_id == seg.segment_id for m in prepared.periods) != 1:
            reasons.add(VerificationReason.CONTEXT_AMBIGUOUS)
        if not any(
            proposed_span(m.span) == proposal.period.span and m.period == proposal.period.period
            for m in prepared.periods
        ):
            reasons.add(VerificationReason.REFERENCE_PERIOD_MISMATCH)
    hard = {
        VerificationReason.SPAN_INVALID,
        VerificationReason.NUMERIC_MISMATCH,
        VerificationReason.ENTITY_MISMATCH,
        VerificationReason.TIME_UNSUPPORTED,
        VerificationReason.NEGATION_CONFLICT,
        VerificationReason.UNIT_MISMATCH,
        VerificationReason.CLASSIFICATION_UNSUPPORTED,
    }
    state = (
        VerificationState.REJECTED
        if reasons & hard
        else VerificationState.UNVERIFIED
        if VerificationReason.ENTITY_UNRESOLVED in reasons
        else VerificationState.PARTIALLY_VERIFIED
        if reasons
        else VerificationState.VERIFIED
    )
    return VerificationResult(
        candidate.candidate_id, state, tuple(sorted(reasons)), tuple(sorted(spans))
    ), tuple(spans[k] for k in sorted(spans))
