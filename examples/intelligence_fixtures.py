"""Synthetic offline sourced financial fields, also used by the profile."""

from hashlib import sha256

from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState
from eie.core.serialization import canonical_json_bytes
from eie.corporate.types import EarningsPeriod
from eie.entities.sec import sec_issuer_id
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import (
    ActionValue,
    Claim,
    ClaimSubject,
    ClaimValue,
    IdentityValue,
    MeasurementSemantics,
    NumericRange,
    OccurrenceValue,
    RelationshipValue,
    StatusValue,
    TimeValue,
)
from eie.evidence.references import (
    AcquisitionLineage,
    EvidenceDataset,
    EvidenceLocator,
    LocatorKind,
)
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate, SubjectKind
from eie.expectations.models import MeasurementContext, MeasurementId
from eie.expectations.observations import ActualRole
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.ingestion.models import RetentionPermission
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference
from eie.providers.sec.models import CIK
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

ISSUER = sec_issuer_id(CIK("0000320193"))
DATASET = EvidenceDataset(
    "offline-intelligence", fingerprint("eie:intelligence-demo-dataset:v1", "synthetic")
)
USD = Unit(UnitKind.CURRENCY, currency="USD")
DEFAULT_PERIOD = EarningsPeriod(2026, 3, "2026-09-30")


def source(
    name: str = "PRIMARY",
    *,
    family: str | None = None,
    source_class: SourceClass = SourceClass.COMPANY_PRIMARY,
) -> EvidenceProvider:
    return EvidenceProvider(
        SourceIdentity(name, "1", "1", source_class),
        family or name,
        DATASET,
        frozenset(ClaimType),
        normalized_retention=RetentionPermission.ALLOWED,
    )


def batch(
    provider: EvidenceProvider,
    known: int,
    value: ClaimValue | None = None,
    *,
    key: str | None = None,
    period: EarningsPeriod = DEFAULT_PERIOD,
    measurement: str = "EARNINGS.REVENUE",
    prior: EvidenceBatch | None = None,
    status: ClaimStatus = ClaimStatus.ASSERTED,
    event_type: EventType | None = None,
    subject_id: str = ISSUER,
    semantics: MeasurementSemantics | None = None,
) -> EvidenceBatch:
    value = value or Quantity(500_000_000, USD)
    number = 1 if prior is None else prior.events[0].revision.number + 1
    key = key or f"financial-{period.key}"
    raw_bytes = canonical_json_bytes((key, number, value))
    raw = RawSourceObservation(
        provider.source,
        key,
        str(number),
        sha256(raw_bytes).hexdigest(),
        "application/json",
        known,
        known,
    )
    event = CanonicalEvent(
        EventIdentity(
            event_type or EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE"),
            EventScope(ScopeKind.COMPANY, (subject_id,)),
            key,
        ),
        EventRevision(
            number,
            RevisionKind.ORIGINAL if prior is None else RevisionKind.CORRECTION,
            known,
            None if prior is None else number - 1,
        ),
        LifecycleState.RELEASED if prior is None else LifecycleState.UPDATED,
        ConfirmationState.CONFIRMED_PRIMARY,
        (raw.observation_id,),
        frozenset({provider.source.source_class}),
        "Synthetic financial release",
        published_at_ns=known,
    )
    lineage = AcquisitionLineage(
        raw.observation_id,
        known,
        known,
        provider.parser_version,
        fingerprint("eie:intelligence-fixture-settings:v1", "synthetic"),
    )
    ref = field_reference(
        raw.observation_id,
        key,
        raw.payload_hash,
        provider.parser_version,
        provider.source.adapter_version,
        EvidenceLocator(LocatorKind.JSON_POINTER, "/value"),
        value,
    )
    if isinstance(value, (Quantity, NumericRange)):
        claim_type = (
            ClaimType.GUIDANCE_VALUE
            if measurement.startswith("GUIDANCE.")
            else ClaimType.ACTUAL_VALUE
        )
        claim = Claim(
            claim_type,
            ClaimSubject(SubjectKind.ENTITY, subject_id),
            Predicate.GUIDANCE if claim_type is ClaimType.GUIDANCE_VALUE else Predicate.ACTUAL,
            MeasurementContext(event.event_id, MeasurementId(measurement), period),
            value.canonical_unit,
            semantics
            or (
                MeasurementSemantics(actual_role=ActualRole.ACTUAL)
                if claim_type is ClaimType.ACTUAL_VALUE
                else None
            ),
        )
    else:
        kind, predicate = (
            (ClaimType.EVENT_STATUS, Predicate.STATUS)
            if isinstance(value, StatusValue)
            else (ClaimType.EVENT_TIME, Predicate.EFFECTIVE_AT)
            if isinstance(value, TimeValue)
            else (ClaimType.EVENT_OCCURRENCE, Predicate.OCCURRED)
            if isinstance(value, OccurrenceValue)
            else (ClaimType.CORPORATE_ACTION, Predicate.ACTION)
            if isinstance(value, ActionValue)
            else (ClaimType.ENTITY_IDENTITY, Predicate.IDENTIFIES)
            if isinstance(value, IdentityValue)
            else (ClaimType.RELATIONSHIP, Predicate.RELATES_TO)
            if isinstance(value, RelationshipValue)
            else (ClaimType.FILING_METADATA, Predicate.FILING)
        )
        claim = Claim(kind, ClaimSubject(SubjectKind.EVENT, event.event_id), predicate)
    return EvidenceBatch(
        "financial",
        raw,
        lineage,
        (ref,),
        (
            ClaimInput(
                key + ":value", claim, value, (ref.evidence_id,), status, event_id=event.event_id
            ),
        ),
        (event,),
    )
