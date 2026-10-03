"""Source-local event envelopes and exact fact slots feed EIE-11's resolution engine."""

from dataclasses import fields
from decimal import Decimal

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState
from eie.corporate.actions import Buyback, Dividend, ManagementChange
from eie.corporate.models import CorporateIdentity, CorporatePayload
from eie.corporate.types import CorporateKind, Money
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventType
from eie.evidence.claims import (
    ActionValue,
    Claim,
    ClaimSubject,
    ClaimValue,
    MeasurementSemantics,
    NumericRange,
    OccurrenceValue,
)
from eie.evidence.matching import EventDescriptor
from eie.evidence.references import EvidenceLocator, EvidenceReference, LocatorKind, SpanKind
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate, SubjectKind
from eie.expectations.models import MeasurementContext, MeasurementId
from eie.expectations.observations import ActualRole
from eie.expectations.units import UnitKind
from eie.nlp.classification import event_type
from eie.nlp.documents import ExtractionAcquisition, TextDocument
from eie.nlp.extraction import ExtractionArtifacts, management_source
from eie.nlp.models import ExtractedEventCandidate
from eie.nlp.schema import (
    ExtractionMethod,
    FieldName,
    NumericBound,
    SourceAction,
    UnscheduledFamily,
    VerificationState,
)
from eie.nlp.spans import EvidenceSpan
from eie.providers.evidence.models import ClaimInput, EvidenceBatch


def acquisition(artifacts: ExtractionArtifacts) -> ExtractionAcquisition:
    source = artifacts.prepared.document.source_lineage
    return ExtractionAcquisition(
        **{f.name: getattr(source, f.name) for f in fields(source)},
        extraction_available_ns=artifacts.run.available_ns,
        processing_fingerprint=artifacts.run.processing_fingerprint,
    )


def reference(artifacts: ExtractionArtifacts, span: EvidenceSpan) -> EvidenceReference:
    doc = artifacts.prepared.document
    return source_reference(doc, len(artifacts.prepared.original), span)


def source_reference(
    doc: TextDocument, original_characters: int, span: EvidenceSpan
) -> EvidenceReference:
    return EvidenceReference(
        doc.raw.observation_id,
        doc.raw.source_item_id,
        doc.raw.payload_hash,
        span.original_fragment_hash,
        doc.source_lineage.parser_version,
        doc.raw.source.adapter_version,
        EvidenceLocator(
            LocatorKind.FIELD,
            "source-text",
            span_kind=SpanKind.CHARACTER,
            start=span.original_start,
            end=span.original_end,
            content_length=original_characters,
        ),
    )


def source_key(artifacts: ExtractionArtifacts, candidate: ExtractedEventCandidate) -> str:
    seg = next(s for s in artifacts.prepared.segments if s.start == candidate.proposal.span.start)
    if candidate.proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE:
        p = management_source(artifacts.prepared, candidate.proposal)
        if p is None:
            raise ValidationError("management candidate lacks one grounded source identity")
        identity = tuple((f.name, f.value.casefold()) for f in p.text_fields)
        complete = {f.name for f in p.text_fields} == {
            FieldName.ROLE,
            FieldName.PERSON,
            FieldName.ACTION,
        } and p.temporal is not None
        return "management_change:" + fingerprint(
            "eie:text-management-slot:v1",
            (
                p.entity_ids,
                identity,
                None if p.temporal is None else p.temporal.value,
                None if complete else p.key,
            ),
        )
    return f"{candidate.proposal.family.value.lower()}:{seg.index}"


def action_payload(candidate: ExtractedEventCandidate) -> CorporatePayload | None:
    p = candidate.proposal
    amount = next(
        (n for n in p.numerics if n.name is FieldName.AMOUNT and n.bound is NumericBound.POINT),
        None,
    )
    money = None
    if (
        amount is not None
        and amount.quantity.value >= 0
        and amount.quantity.unit.kind in (UnitKind.CURRENCY, UnitKind.CURRENCY_PER_SHARE)
    ):
        micros = (
            Decimal(str(amount.quantity.value)) * Decimal(amount.quantity.unit.factor) * 1_000_000
        )
        if micros == micros.to_integral_value():
            assert amount.quantity.unit.currency is not None
            money = Money(
                int(micros),
                amount.quantity.unit.currency,
                amount.quantity.unit.kind is UnitKind.CURRENCY_PER_SHARE,
            )
    if p.family is UnscheduledFamily.BUYBACK and money is not None and not money.per_share:
        return Buyback(money)
    if p.family is UnscheduledFamily.DIVIDEND and money is not None and money.per_share:
        return Dividend(money)
    text = {f.name: f.value for f in p.text_fields}
    if (
        p.family is UnscheduledFamily.MANAGEMENT_CHANGE
        and FieldName.ROLE in text
        and FieldName.PERSON in text
    ):
        return ManagementChange(
            text[FieldName.PERSON],
            text[FieldName.ROLE],
            effective_date=None if p.temporal is None else p.temporal.value.date,
        )
    return None


def grounded_claims(
    candidate: ExtractedEventCandidate,
    event_id: str,
    typ: EventType,
) -> tuple[tuple[str, Claim, ClaimValue], ...]:
    cp = candidate.proposal
    result: list[tuple[str, Claim, ClaimValue]] = []

    def put(suffix: str, claim: Claim, value: ClaimValue) -> None:
        result.append((suffix, claim, value))

    subject = ClaimSubject(SubjectKind.EVENT, event_id)
    put(
        "occurrence",
        Claim(ClaimType.EVENT_OCCURRENCE, subject, Predicate.OCCURRED),
        OccurrenceValue(True),
    )
    if cp.temporal is not None:
        put(
            "time",
            Claim(ClaimType.EVENT_TIME, subject, Predicate.EFFECTIVE_AT),
            cp.temporal.value,
        )
    for field in cp.numerics:
        if field.name is FieldName.AMOUNT:
            # EIE-5 has no generic monetary metric; retain the grounded field.
            continue
        assert cp.period is not None and len(cp.entity_ids) == 1
        guide = cp.family is UnscheduledFamily.GUIDANCE_CHANGE
        code = ("GUIDANCE." if guide else "EARNINGS.") + {
            FieldName.REVENUE: "REVENUE",
            FieldName.GUIDANCE_REVENUE: "REVENUE",
            FieldName.EPS: "EPS",
            FieldName.GUIDANCE_EPS: "EPS",
            FieldName.MARGIN: "MARGIN",
        }[field.name]
        if field.qualifiers:
            code += "." + ".".join(q.value for q in field.qualifiers)
        # A source-neutral exact issuer/period referent identifies the measurement
        # proposition. EIE-11 retains the separate source event envelopes.
        referent = EventIdentity(
            typ, EventScope(ScopeKind.COMPANY, cp.entity_ids), cp.period.period.key
        )
        context = MeasurementContext(referent.event_id, MeasurementId(code), cp.period.period)
        numeric_claim = Claim(
            ClaimType.GUIDANCE_VALUE if guide else ClaimType.ACTUAL_VALUE,
            ClaimSubject(SubjectKind.ENTITY, cp.entity_ids[0]),
            Predicate.GUIDANCE if guide else Predicate.ACTUAL,
            context,
            field.quantity.canonical_unit,
            None if guide else MeasurementSemantics(actual_role=ActualRole.ACTUAL),
        )
        value: ClaimValue = (
            field.quantity
            if field.bound is NumericBound.POINT
            else NumericRange(
                None if field.bound is NumericBound.AT_MOST else field.quantity,
                field.quantity if field.bound is NumericBound.AT_MOST else field.upper,
            )
        )
        put(field.name.value.lower(), numeric_claim, value)
    payload = action_payload(candidate)
    if payload is not None:
        put(
            "action",
            Claim(ClaimType.CORPORATE_ACTION, subject, Predicate.ACTION),
            ActionValue(payload),
        )
    return tuple(result)


def batch(
    repo: EvidenceRepository,
    artifacts: ExtractionArtifacts,
) -> tuple[EvidenceBatch, dict[str, tuple[str, tuple[str, ...]]]]:
    doc, run = artifacts.prepared.document, artifacts.run
    verified_ids = {
        v.candidate_id: v.span_ids
        for v in artifacts.verifications
        if v.state is VerificationState.VERIFIED
    }
    span_by_id = {s.span_id: s for s in artifacts.spans}
    refs: dict[str, EvidenceReference] = {}
    inputs: dict[str, ClaimInput] = {}
    events: dict[str, CanonicalEvent] = {}
    descriptors: dict[str, EventDescriptor] = {}
    links: dict[str, tuple[str, tuple[str, ...]]] = {}
    groups: dict[str, list[ExtractedEventCandidate]] = {}
    for c in artifacts.candidates:
        if c.candidate_id in verified_ids:
            groups.setdefault(source_key(artifacts, c), []).append(c)
    for key, candidates in sorted(groups.items()):
        p = next(
            c.proposal
            for c in artifacts.candidates
            if c.method is ExtractionMethod.DETERMINISTIC and source_key(artifacts, c) == key
        )
        text = (
            " ".join(f.value for f in p.text_fields)
            if p.family is UnscheduledFamily.MANAGEMENT_CHANGE
            else artifacts.prepared.normalized.text[p.span.start : p.span.end]
        )
        typ = event_type(p.family, text)
        identity = EventIdentity(
            typ,
            EventScope(ScopeKind.COMPANY, p.entity_ids),
            fingerprint(
                "eie:text-source-event:v1",
                (
                    doc.thread_id,
                    key,
                    None if p.period is None else p.period.period.key,
                    tuple(
                        (f.name, f.value)
                        for f in p.text_fields
                        if f.name in (FieldName.PERSON, FieldName.ROLE)
                    ),
                ),
            ),
        )
        prior = repo.store.get_event(identity.event_id, run.available_ns)
        if prior is not None and prior.revision.available_from_ns >= run.available_ns:
            raise ValidationError("source-local event reprocessing must advance availability")
        number = 1 if prior is None else prior.revision.number + 1
        event = CanonicalEvent(
            identity,
            EventRevision(
                number,
                RevisionKind.ORIGINAL
                if prior is None
                else RevisionKind.CORRECTION
                if doc.action is SourceAction.CORRECTED
                else RevisionKind.UPDATE,
                run.available_ns,
                None if prior is None else number - 1,
            ),
            LifecycleState.RELEASED if prior is None else LifecycleState.UPDATED,
            ConfirmationState.REPORTED,
            (doc.raw.observation_id,),
            frozenset({doc.raw.source.source_class}),
            p.family.value.replace("_", " ").title(),
            source_observed_at_ns=doc.raw.source_observed_at_ns,
            ingested_at_ns=doc.raw.ingested_at_ns,
        )
        events[event.revision_id] = event
        # EIE-11 compares these exact source-local descriptors and decides aliases.
        descriptor_key = fingerprint("eie:text-descriptor-key:v1", (doc.raw.source_item_id, key))
        previous_descriptors = tuple(
            d
            for d in repo.descriptors(doc.dataset_id, run.available_ns)
            if d.source.name == doc.raw.source.name
            and d.resource_id == doc.resource_id
            and d.provider_event_key == descriptor_key
        )
        previous = (
            max(previous_descriptors, key=lambda d: d.number) if previous_descriptors else None
        )
        period = p.period.period if p.period else None
        corporate = (
            CorporateIdentity(CorporateKind.EARNINGS_RELEASE, p.entity_ids[0], period.key)
            if period and len(p.entity_ids) == 1 and p.family is UnscheduledFamily.EARNINGS_RESULT
            else None
        )
        descriptor = EventDescriptor(
            event.event_id,
            number,
            typ,
            doc.raw.source,
            doc.dataset_id,
            doc.resource_id,
            descriptor_key,
            doc.raw.observation_id,
            run.available_ns,
            tuple(ClaimSubject(SubjectKind.ENTITY, i) for i in p.entity_ids),
            1 if previous is None else previous.number + 1,
            None if previous is None else previous.descriptor_id,
            period,
            None if p.temporal is None else p.temporal.value,
            corporate_identity=corporate,
            lifecycle=event.lifecycle,
        )
        descriptors[descriptor.descriptor_id] = descriptor
        for candidate in candidates:
            evidence_ids = tuple(
                sorted(
                    {
                        reference(artifacts, span_by_id[s]).evidence_id
                        for s in verified_ids[candidate.candidate_id]
                    }
                )
            )
            for sid in verified_ids[candidate.candidate_id]:
                ref = reference(artifacts, span_by_id[sid])
                refs[ref.evidence_id] = ref
            candidate_claims: list[str] = []
            claim_status = (
                ClaimStatus.CORRECTED
                if doc.action is SourceAction.CORRECTED
                else ClaimStatus.UPDATED
                if prior is not None
                else ClaimStatus.ASSERTED
            )

            def put(
                suffix: str,
                claim: Claim,
                value: ClaimValue,
                *,
                event_id: str = event.event_id,
                group_key: str = key,
                ref_ids: tuple[str, ...] = evidence_ids,
                collected: list[str] = candidate_claims,
                status: ClaimStatus = claim_status,
            ) -> None:
                claim_key = doc.raw.source_item_id + ":" + group_key + ":" + suffix
                if len(claim_key) > 256:
                    claim_key = fingerprint("eie:text-claim-key:v1", claim_key)
                item = ClaimInput(
                    claim_key,
                    claim,
                    value,
                    ref_ids,
                    status,
                    event_id=event_id,
                )
                old = inputs.get(claim_key)
                if old is not None:
                    if old.claim != item.claim or old.value != item.value:
                        raise ValidationError(
                            "verified proposals disagree on a source-local fact slot"
                        )
                    item = ClaimInput(
                        claim_key,
                        claim,
                        value,
                        tuple(sorted(set(old.evidence_ids) | set(ref_ids))),
                        item.status,
                        event_id=event_id,
                    )
                inputs[claim_key] = item
                collected.append(claim.claim_id)

            for suffix, claim, value in grounded_claims(candidate, event.event_id, typ):
                put(suffix, claim, value)
            links[candidate.candidate_id] = (event.event_id, tuple(sorted(set(candidate_claims))))
    if len(inputs) > run.settings.normalization.limits.accepted_claims:
        raise ValidationError("accepted claim bound exceeded")
    return EvidenceBatch(
        doc.resource_id,
        doc.raw,
        acquisition(artifacts),
        tuple(refs[k] for k in sorted(refs)),
        tuple(inputs[k] for k in sorted(inputs)),
        tuple(events[k] for k in sorted(events)),
        tuple(descriptors[k] for k in sorted(descriptors)),
    ), links
