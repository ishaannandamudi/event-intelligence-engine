"""Exact structured blocking and conservative, event-specific matching."""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.corporate.models import CorporateIdentity
from eie.corporate.types import CorporateStatus
from eie.events.lifecycle import LifecycleState
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import ClaimSubject, TimeValue, identity
from eie.evidence.schema import DedupeDecision, MatchReason, SubjectKind
from eie.expectations.models import Period
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract


class PartyRole(StrEnum):
    TARGET = "TARGET"
    ACQUIRER = "ACQUIRER"
    SPUN_ENTITY = "SPUN_ENTITY"


@dataclass(frozen=True, slots=True)
class EventParty:
    role: PartyRole
    entity: ClaimSubject

    def __post_init__(self) -> None:
        contract(self)
        if self.entity.kind is not SubjectKind.ENTITY:
            raise ValidationError("event party requires stable entity")


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    event_id: str
    event_revision: int
    event_type: EventType
    source: SourceIdentity
    dataset_id: str
    resource_id: str
    provider_event_key: str
    raw_observation_id: str
    known_from_ns: int
    entities: tuple[ClaimSubject, ...]
    number: int = 1
    previous_id: str | None = None
    period: Period | None = None
    time: TimeValue | None = None
    accession: str | None = None
    corporate_identity: CorporateIdentity | None = None
    effective_date: str | None = None
    parties: tuple[EventParty, ...] = ()
    release_stage: str | None = None
    official_release_id: str | None = None
    lifecycle: LifecycleState | CorporateStatus | None = None

    def __post_init__(self) -> None:
        contract(self)
        identity(self.event_id, ("evt",))
        identity(self.dataset_id, ("eds",))
        identity(self.raw_observation_id, ("obs",))
        if (
            self.event_revision < 1
            or self.number < 1
            or (self.number == 1) != (self.previous_id is None)
        ):
            raise ValidationError("event descriptor revision chain required")
        if self.previous_id is not None:
            identity(self.previous_id, ("edesc",))
        if (
            len(self.entities) > 16
            or tuple(e.identity for e in self.entities)
            != tuple(sorted({e.identity for e in self.entities}))
            or any(e.kind is not SubjectKind.ENTITY for e in self.entities)
        ):
            raise ValidationError("bounded sorted distinct stable event entities required")
        if len(self.parties) > 3 or tuple(p.role for p in self.parties) != tuple(
            sorted({p.role for p in self.parties})
        ):
            raise ValidationError("distinct ordered event party roles required")
        if self.accession is not None and (
            self.event_type.family is not EventFamily.SEC_FILING
            or re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", self.accession) is None
        ):
            raise ValidationError("exact SEC accession required")
        if self.effective_date is not None:
            try:
                if date.fromisoformat(self.effective_date).isoformat() != self.effective_date:
                    raise ValueError
            except ValueError as exc:
                raise ValidationError("ISO effective date required") from exc
        if self.corporate_identity is not None:
            if (
                self.corporate_identity.issuer_id not in {e.identity for e in self.entities}
                or self.period is not None
                and self.corporate_identity.semantic_key != getattr(self.period, "key", None)
            ):
                raise ValidationError("EIE-4 identity disagrees with issuer/period")
        if any(p.entity not in self.entities for p in self.parties):
            raise ValidationError("event parties must be retained in entity applicability")

    @property
    def thread_id(self) -> str:
        return "ethread_" + fingerprint(
            "eie:event-source-thread:v1",
            (self.dataset_id, self.source.name, self.resource_id, self.provider_event_key),
        )

    @property
    def descriptor_id(self) -> str:
        return "edesc_" + fingerprint("eie:event-descriptor:v1", self)


@dataclass(frozen=True, slots=True)
class DedupePolicy:
    tolerances: tuple[tuple[str, int], ...] = (
        ("EARNINGS.CALL", 300_000_000_000),
        ("EARNINGS.RELEASE", 300_000_000_000),
        ("EARNINGS.SCHEDULE", 300_000_000_000),
        ("MACRO.CPI.US", 60_000_000_000),
        ("MACRO.NFP.US", 60_000_000_000),
    )
    version: str = "structured-matching-1"

    def __post_init__(self) -> None:
        contract(self)
        if (
            len(self.tolerances) > 128
            or tuple(k for k, _ in self.tolerances)
            != tuple(sorted({k for k, _ in self.tolerances}))
            or any(not 0 <= v <= 86_400_000_000_000 for _, v in self.tolerances)
        ):
            raise ValidationError("bounded explicit event-type tolerances required")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:dedupe-policy:v1", self)


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    left_id: str
    right_id: str
    left_event_id: str
    right_event_id: str
    decision: DedupeDecision
    matches: tuple[MatchReason, ...]
    conflicts: tuple[MatchReason, ...]
    time_delta_ns: int | None
    policy: DedupePolicy

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.left_id >= self.right_id
            or self.time_delta_ns is not None
            and self.time_delta_ns < 0
        ):
            raise ValidationError("canonical duplicate pair and nonnegative delta required")
        if self.matches != tuple(sorted(set(self.matches))) or self.conflicts != tuple(
            sorted(set(self.conflicts))
        ):
            raise ValidationError("deterministic candidate reasons required")
        if self.decision is DedupeDecision.MERGE and self.conflicts:
            raise ValidationError("conflicting pair cannot merge")

    @property
    def candidate_id(self) -> str:
        return "dcand_" + fingerprint("eie:duplicate-candidate:v1", self)


def blocks(row: EventDescriptor) -> tuple[str, ...]:
    keys: set[str] = {"semantic:" + row.event_id, "provider:" + row.thread_id}
    if row.accession is not None:
        keys.add("accession:" + row.accession)
    if row.corporate_identity is not None:
        keys.add("corporate:" + row.corporate_identity.corporate_id)
    if row.official_release_id is not None:
        keys.add(
            "release:"
            + fingerprint("eie:release-block:v1", (row.event_type, row.official_release_id))
        )
    for entity in row.entities:
        if row.period is not None:
            keys.add(
                "period:" + fingerprint("eie:period-block:v1", (entity, row.event_type, row.period))
            )
        if row.time is not None and row.time.date is not None:
            day = date.fromisoformat(row.time.date).toordinal()
            # Adjacent buckets catch tolerances crossing UTC midnight without inventing precision.
            for bucket in (day - 1, day, day + 1):
                keys.add(
                    "date:" + fingerprint("eie:date-block:v1", (entity, row.event_type, bucket))
                )
    if (
        not row.entities
        and row.event_type.family in (EventFamily.MACRO, EventFamily.CENTRAL_BANK)
        and row.period is not None
    ):
        keys.add(
            "macro:"
            + fingerprint("eie:macro-block:v1", (row.event_type, row.period, row.release_stage))
        )
    return tuple(sorted(keys))


DEFAULT_DEDUPE_POLICY = DedupePolicy()


def match_events(
    left: EventDescriptor, right: EventDescriptor, policy: DedupePolicy = DEFAULT_DEDUPE_POLICY
) -> DuplicateCandidate:
    if left.dataset_id != right.dataset_id or left.descriptor_id == right.descriptor_id:
        raise ValidationError("distinct events from the same evidence dataset required")
    left, right = sorted((left, right), key=lambda r: r.descriptor_id)
    matches: set[MatchReason] = set()
    conflicts: set[MatchReason] = set()
    if left.event_type != right.event_type:
        conflicts.add(MatchReason.INCOMPATIBLE_TYPE)
    if left.entities and right.entities:
        (matches if left.entities == right.entities else conflicts).add(
            MatchReason.SAME_ENTITY
            if left.entities == right.entities
            else MatchReason.DIFFERENT_ENTITY
        )
    if left.period is not None and right.period is not None:
        (matches if left.period == right.period else conflicts).add(
            MatchReason.SAME_REPORTING_PERIOD
            if left.period == right.period
            else MatchReason.DIFFERENT_PERIOD
        )
    if left.accession is not None and right.accession is not None:
        (matches if left.accession == right.accession else conflicts).add(
            MatchReason.EXACT_ACCESSION
            if left.accession == right.accession
            else MatchReason.DISTINCT_ACCESSION
        )
    if left.corporate_identity is not None and right.corporate_identity is not None:
        (matches if left.corporate_identity == right.corporate_identity else conflicts).add(
            MatchReason.EXACT_CORPORATE_IDENTITY
            if left.corporate_identity == right.corporate_identity
            else MatchReason.DISTINCT_CORPORATE_IDENTITY
        )
    if (
        left.effective_date is not None
        and right.effective_date is not None
        and left.effective_date != right.effective_date
    ):
        conflicts.add(MatchReason.DISTINCT_EFFECTIVE_DATE)
    if left.parties and right.parties and left.parties != right.parties:
        conflicts.add(MatchReason.CONFLICTING_PARTIES)
    if (
        left.release_stage is not None
        and right.release_stage is not None
        and left.release_stage != right.release_stage
    ):
        conflicts.add(MatchReason.DISTINCT_RELEASE_STAGE)
    if (
        left.official_release_id is not None
        and right.official_release_id is not None
        and left.official_release_id != right.official_release_id
    ):
        conflicts.add(MatchReason.DISTINCT_RELEASE_IDENTITY)
    if left.event_id == right.event_id:
        matches.add(MatchReason.EXACT_EVENT_IDENTITY)
    if left.thread_id == right.thread_id:
        matches.add(MatchReason.PROVIDER_LINKAGE)
    if (
        left.official_release_id is not None
        and left.official_release_id == right.official_release_id
    ):
        matches.add(MatchReason.OFFICIAL_RELEASE_IDENTITY)
    terminals = {
        CorporateStatus.CLOSED,
        CorporateStatus.TERMINATED,
        CorporateStatus.CANCELLED,
        CorporateStatus.WITHDRAWN,
        LifecycleState.CANCELLED,
    }
    if (
        left.lifecycle in terminals
        and right.lifecycle in terminals
        and left.lifecycle != right.lifecycle
    ):
        conflicts.add(MatchReason.INCOMPATIBLE_LIFECYCLE)
    exact = {
        MatchReason.EXACT_EVENT_IDENTITY,
        MatchReason.EXACT_ACCESSION,
        MatchReason.EXACT_CORPORATE_IDENTITY,
        MatchReason.PROVIDER_LINKAGE,
    }
    strong = bool(matches & exact)
    if (
        left.event_type.family is EventFamily.EARNINGS
        and MatchReason.SAME_ENTITY in matches
        and MatchReason.SAME_REPORTING_PERIOD in matches
    ):
        strong = True
    if (
        left.event_type.family in (EventFamily.MACRO, EventFamily.CENTRAL_BANK)
        and MatchReason.SAME_REPORTING_PERIOD in matches
        and MatchReason.OFFICIAL_RELEASE_IDENTITY in matches
        and left.release_stage == right.release_stage
    ):
        strong = True
    deal = left.event_type.code in ("CORPORATE_ACTION.MERGER", "CORPORATE_ACTION.ACQUISITION")
    if deal and (
        not left.parties
        or left.parties != right.parties
        or {p.role for p in left.parties} != {PartyRole.TARGET, PartyRole.ACQUIRER}
    ):
        strong = False
    delta = None
    if left.time is not None and right.time is not None:
        a, b = left.time.timestamp_ns, right.time.timestamp_ns
        if a is not None and b is not None:
            delta = abs(a - b)
            if delta <= dict(policy.tolerances).get(left.event_type.code, 0):
                matches.add(MatchReason.TIME_WITHIN_TOLERANCE)
            elif strong:
                matches.add(MatchReason.STABLE_IDENTITY_TIME_CHANGE)
            else:
                conflicts.add(MatchReason.TIME_OUTSIDE_TOLERANCE)
        else:
            matches.add(MatchReason.COARSE_TIME)
    if conflicts:
        decision = DedupeDecision.KEEP_SEPARATE
    elif strong:
        decision = DedupeDecision.MERGE
    else:
        decision = DedupeDecision.UNRESOLVED
        matches.add(MatchReason.INSUFFICIENT_IDENTITY)
    return DuplicateCandidate(
        left.descriptor_id,
        right.descriptor_id,
        left.event_id,
        right.event_id,
        decision,
        tuple(sorted(matches)),
        tuple(sorted(conflicts)),
        delta,
        policy,
    )
