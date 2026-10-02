"""Directed typed claim links with causal, semantic validation."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.lifecycle import LifecycleState, validate_transition
from eie.evidence.claims import ProviderClaimObservation, StatusValue, TimeValue, identity
from eie.evidence.schema import ClaimStatus, EvidenceRelation
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class ClaimRelationship:
    source_id: str
    target_id: str
    relation: EvidenceRelation
    known_from_ns: int

    def __post_init__(self) -> None:
        contract(self)
        identity(self.source_id, ("pclaim",))
        identity(self.target_id, ("pclaim",))
        if self.source_id == self.target_id:
            raise ValidationError("claim relationships require distinct observations")

    @property
    def relationship_id(self) -> str:
        return "crel_" + fingerprint("eie:claim-relationship:v1", self)


def validate_relationship(
    link: ClaimRelationship, source: ProviderClaimObservation, target: ProviderClaimObservation
) -> None:
    from eie.evidence.resolution import compatible, informative

    if (
        (link.source_id, link.target_id) != (source.observation_id, target.observation_id)
        or source.dataset_id != target.dataset_id
        or link.known_from_ns < max(source.known_from_ns, target.known_from_ns)
    ):
        raise ValidationError("claim relationship reference/dataset/knowledge mismatch")
    duplicate = link.relation in (
        EvidenceRelation.DUPLICATE_OF,
        EvidenceRelation.POSSIBLE_DUPLICATE_OF,
    )
    if (
        not duplicate
        and link.relation not in (EvidenceRelation.CORRECTS, EvidenceRelation.SUPERSEDES)
        and source.claim_id != target.claim_id
    ):
        raise ValidationError("competing claims require the same semantic proposition")
    change = link.relation in (
        EvidenceRelation.CORRECTS,
        EvidenceRelation.UPDATES,
        EvidenceRelation.RETRACTS,
        EvidenceRelation.SUPERSEDES,
    )
    if change:
        if (
            source.thread_id != target.thread_id
            or source.previous_id != target.observation_id
            or source.known_from_ns <= target.known_from_ns
        ):
            raise ValidationError("change relationship must point to immediate causal predecessor")
        required = {
            EvidenceRelation.CORRECTS: (ClaimStatus.CORRECTED,),
            EvidenceRelation.UPDATES: (ClaimStatus.UPDATED,),
            EvidenceRelation.RETRACTS: (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN),
        }
        if link.relation in required and source.status not in required[link.relation]:
            raise ValidationError("claim action and relationship disagree")
    if link.relation in (
        EvidenceRelation.SUPPORTS,
        EvidenceRelation.CONFIRMS,
        EvidenceRelation.CONTRADICTS,
    ) and not all(informative(value) for value in (source.value, target.value)):
        raise ValidationError("unknown time cannot support, confirm or contradict")
    if link.relation in (EvidenceRelation.SUPPORTS, EvidenceRelation.CONFIRMS) and not compatible(
        source.value, target.value
    ):
        raise ValidationError("support cannot assert conflicting values")
    if link.relation is EvidenceRelation.CONTRADICTS and compatible(source.value, target.value):
        raise ValidationError("contradiction requires incompatible assertions")
    if link.relation is EvidenceRelation.CONFIRMS and source.source_family == target.source_family:
        raise ValidationError("independent confirmation requires distinct source families")


def validate_revision(
    current: ProviderClaimObservation, previous: ProviderClaimObservation | None
) -> None:
    if previous is None:
        if current.number != 1:
            raise ValidationError("provider claim predecessor is missing")
        return
    if (
        current.thread_id != previous.thread_id
        or current.number != previous.number + 1
        or current.previous_id != previous.observation_id
        or current.known_from_ns <= previous.known_from_ns
        or current.source_family != previous.source_family
    ):
        raise ValidationError("provider claim revision chain is inconsistent")
    if current.status in (ClaimStatus.ASSERTED, ClaimStatus.CONFIRMED):
        raise ValidationError(
            "later provider assertion needs explicit update/correction/retraction"
        )
    if current.claim_id != previous.claim_id and current.status is not ClaimStatus.CORRECTED:
        raise ValidationError("semantic claim reassignment requires correction")
    if (
        current.status is ClaimStatus.UPDATED
        and isinstance(current.value, TimeValue)
        and isinstance(previous.value, TimeValue)
    ):
        # Refinements and reschedules are explicit sourced updates, not inferred ordering.
        return
    if (
        current.status is ClaimStatus.UPDATED
        and isinstance(current.value, StatusValue)
        and isinstance(previous.value, StatusValue)
    ):
        if type(current.value.status) is not type(previous.value.status):
            raise ValidationError("status domains cannot be mixed")
        if isinstance(current.value.status, LifecycleState) and isinstance(
            previous.value.status, LifecycleState
        ):
            validate_transition(previous.value.status, current.value.status)
        else:
            from eie.corporate.types import CorporateStatus

            allowed = {
                CorporateStatus.ANNOUNCED: {
                    CorporateStatus.PENDING,
                    CorporateStatus.APPROVED,
                    CorporateStatus.CLOSED,
                    CorporateStatus.CANCELLED,
                    CorporateStatus.TERMINATED,
                    CorporateStatus.WITHDRAWN,
                },
                CorporateStatus.SCHEDULED: {CorporateStatus.ANNOUNCED, CorporateStatus.CANCELLED},
                CorporateStatus.PENDING: {
                    CorporateStatus.APPROVED,
                    CorporateStatus.CLOSED,
                    CorporateStatus.CANCELLED,
                    CorporateStatus.TERMINATED,
                },
                CorporateStatus.APPROVED: {CorporateStatus.CLOSED, CorporateStatus.TERMINATED},
            }
            if (
                not isinstance(previous.value.status, CorporateStatus)
                or current.value.status != previous.value.status
                and current.value.status not in allowed.get(previous.value.status, set())
            ):
                raise ValidationError("invalid corporate status update")
