"""Categorical field resolution retains alternatives and equal-rank ambiguity."""

from dataclasses import fields, replace
from itertools import combinations

from eie.backfill.models import ProvenanceKind
from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.evidence.claims import (
    Claim,
    ClaimValue,
    IdentityValue,
    NumericRange,
    ProviderClaimObservation,
    RelationshipValue,
    StatusValue,
    TimePrecision,
    TimeValue,
)
from eie.evidence.graph import ClaimRelationship
from eie.evidence.models import (
    DEFAULT_RESOLUTION_POLICY,
    CanonicalClaimResolution,
    ClaimConflict,
    EvidenceSummary,
    ResolutionPolicy,
)
from eie.evidence.schema import ClaimStatus, ConflictType, EvidenceQuality, ResolutionState
from eie.expectations.units import Quantity
from eie.filings.models import FilingMetadata


def compatible(left: ClaimValue, right: ClaimValue) -> bool:
    if isinstance(left, Quantity) and isinstance(right, Quantity):
        return (
            left.canonical_unit == right.canonical_unit
            and left.canonical_value == right.canonical_value
        )
    if isinstance(left, NumericRange) and isinstance(right, NumericRange):
        return all(
            a is None and b is None or a is not None and b is not None and compatible(a, b)
            for a, b in (
                (left.lower, right.lower),
                (left.upper, right.upper),
                (left.point, right.point),
            )
        )
    if isinstance(left, FilingMetadata) and isinstance(right, FilingMetadata):
        ignored = {
            "source_reference",
            "source_observation_id",
            "available_from_ns",
            "revision_number",
            "quality_flags",
        }
        return all(
            getattr(left, f.name) == getattr(right, f.name)
            for f in fields(FilingMetadata)
            if f.name not in ignored
        )
    if isinstance(left, RelationshipValue) and isinstance(right, RelationshipValue):
        exposure = left.exposure
        if exposure is not None and right.exposure is not None:
            exposure = replace(exposure, source_reference=right.exposure.source_reference)
        return replace(left, exposure=exposure) == right
    if isinstance(left, TimeValue) and isinstance(right, TimeValue):
        if TimePrecision.UNKNOWN in (left.precision, right.precision):
            return left == right
        if left.date != right.date:
            return False
        if (
            left.timestamp_ns is not None
            and right.timestamp_ns is not None
            and left.timestamp_ns != right.timestamp_ns
        ):
            return False
        from eie.corporate.types import ReleaseTiming

        return (
            left.session is ReleaseTiming.UNKNOWN
            or right.session is ReleaseTiming.UNKNOWN
            or left.session is right.session
        )
    return left == right


def conflict_kind(left: ClaimValue, right: ClaimValue) -> ConflictType:
    if isinstance(left, (Quantity, NumericRange)) and isinstance(right, (Quantity, NumericRange)):
        lunit = left.canonical_unit
        runit = right.canonical_unit
        if lunit != runit:
            return ConflictType.UNIT
    if isinstance(left, TimeValue) and isinstance(right, TimeValue):
        return ConflictType.TIME
    if isinstance(left, StatusValue) and isinstance(right, StatusValue):
        return ConflictType.STATUS
    if isinstance(left, IdentityValue) and isinstance(right, IdentityValue):
        return ConflictType.ENTITY
    return ConflictType.VALUE


def informative(value: ClaimValue) -> bool:
    return not isinstance(value, TimeValue) or value.precision is not TimePrecision.UNKNOWN


def unique_observations(
    rows: tuple[ProviderClaimObservation, ...],
) -> tuple[ProviderClaimObservation, ...]:
    revisions: dict[tuple[str, int], ProviderClaimObservation] = {}
    for row in rows:
        key = (row.thread_id, row.number)
        prior = revisions.get(key)
        if prior is not None and prior != row:
            raise ValidationError("conflicting provider claim revision inputs")
        revisions[key] = row
    return tuple(sorted(revisions.values(), key=lambda r: r.observation_id))


def current_observations(
    rows: tuple[ProviderClaimObservation, ...], known_as_of_ns: int, valid_at_ns: int | None
) -> tuple[ProviderClaimObservation, ...]:
    require_ns(known_as_of_ns)
    if valid_at_ns is not None:
        require_ns(valid_at_ns)
    latest: dict[str, ProviderClaimObservation] = {}
    for row in unique_observations(tuple(r for r in rows if r.known_from_ns <= known_as_of_ns)):
        if valid_at_ns is not None and (
            row.valid_from_ns is not None
            and valid_at_ns < row.valid_from_ns
            or row.valid_to_ns is not None
            and valid_at_ns >= row.valid_to_ns
        ):
            continue
        old = latest.get(row.thread_id)
        if old is None or row.number > old.number:
            latest[row.thread_id] = row
    return tuple(sorted(latest.values(), key=lambda r: r.observation_id))


def resolve_claim(
    claim: Claim,
    dataset_id: str,
    rows: tuple[ProviderClaimObservation, ...],
    relationships: tuple[ClaimRelationship, ...],
    known_as_of_ns: int,
    valid_at_ns: int | None = None,
    policy: ResolutionPolicy = DEFAULT_RESOLUTION_POLICY,
) -> CanonicalClaimResolution:
    if any(r.dataset_id != dataset_id for r in rows):
        raise ValidationError("claim resolution cannot mix dataset builds")
    visible = unique_observations(tuple(r for r in rows if r.known_from_ns <= known_as_of_ns))
    alternatives = tuple(r for r in visible if r.claim_id == claim.claim_id)
    for row in alternatives:
        claim.validate_value(row.value)
    current = tuple(
        r
        for r in current_observations(visible, known_as_of_ns, valid_at_ns)
        if r.claim_id == claim.claim_id
    )
    active = tuple(
        r for r in current if r.status not in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN)
    )
    if len(active) > 256 or len(alternatives) > 10000:
        raise ValidationError("claim resolution alternative bound exceeded")
    conflicts = tuple(
        sorted(
            (
                ClaimConflict(a.observation_id, b.observation_id, conflict_kind(a.value, b.value))
                for a, b in combinations(active, 2)
                if informative(a.value)
                and informative(b.value)
                and not compatible(a.value, b.value)
            ),
            key=lambda c: (c.left_id, c.right_id),
        )
    )
    selected: ProviderClaimObservation | None = None
    reasons: set[str] = set()
    quality: set[EvidenceQuality] = set()
    if not current:
        state = ResolutionState.ABSENT
    elif not active:
        state = ResolutionState.RETRACTED
        reasons.add("ALL_APPLICABLE_ASSERTIONS_RETRACTED")
    elif not any(informative(r.value) for r in active):
        state = ResolutionState.UNRESOLVED
        reasons.add("UNKNOWN_TIME")
        quality.add(EvidenceQuality.UNRESOLVED)
    else:
        precedence = policy.precedence.order_for(claim.predicate.value)
        informed = tuple(r for r in active if informative(r.value))
        rank = min(precedence.index(r.source.source_class) for r in informed)
        top = tuple(r for r in informed if precedence.index(r.source.source_class) == rank)
        if any(not compatible(a.value, b.value) for a, b in combinations(top, 2)):
            state = ResolutionState.UNRESOLVED
            reasons.add("EQUAL_PRECEDENCE_CONFLICT")
            quality.add(EvidenceQuality.UNRESOLVED)
        else:

            def selection_key(row: ProviderClaimObservation) -> tuple[int, str]:
                precision = (
                    {
                        TimePrecision.UNKNOWN: 0,
                        TimePrecision.DATE_ONLY: 1,
                        TimePrecision.DATE_PLUS_SESSION: 2,
                        TimePrecision.EXACT: 3,
                    }[row.value.precision]
                    if policy.prefer_exact_time and isinstance(row.value, TimeValue)
                    else 0
                )
                return -precision, row.observation_id

            selected = min(top, key=selection_key)
            state = ResolutionState.RESOLVED
            reasons.add("CATEGORICAL_SOURCE_PRECEDENCE")
            if any(r.value != selected.value and compatible(r.value, selected.value) for r in top):
                reasons.add("COMPATIBLE_REFINEMENT")
    if conflicts:
        quality.add(EvidenceQuality.CONFLICTING_SOURCES)
    if any(r.provenance is not ProvenanceKind.LIVE_OBSERVED for r in alternatives):
        quality.add(EvidenceQuality.RECONSTRUCTED)
    if any(
        isinstance(r.value, TimeValue) and r.value.precision is not TimePrecision.EXACT
        for r in active
    ):
        quality.add(EvidenceQuality.COARSE_TIME)
    supporting = tuple(
        r for r in active if selected is not None and compatible(r.value, selected.value)
    )
    families = {r.source_family for r in supporting}
    conflicting_ids = {v for c in conflicts for v in (c.left_id, c.right_id)}
    contradiction_times = [r.known_from_ns for r in active if r.observation_id in conflicting_ids]
    ids = {r.observation_id for r in alternatives}
    links = tuple(
        sorted(
            {
                r.relationship_id
                for r in relationships
                if r.known_from_ns <= known_as_of_ns and (r.source_id in ids or r.target_id in ids)
            }
        )
    )
    summary = EvidenceSummary(
        len(families),
        frozenset(r.source.source_class for r in active),
        max(0, len(families) - 1),
        len(conflicts),
        sum(r.status is ClaimStatus.CORRECTED for r in alternatives),
        sum(r.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN) for r in alternatives),
        len(conflicts) if state is ResolutionState.UNRESOLVED else 0,
        max((r.known_from_ns for r in supporting), default=None),
        max(contradiction_times, default=None),
    )
    return CanonicalClaimResolution(
        claim.claim_id,
        dataset_id,
        known_as_of_ns,
        valid_at_ns,
        state,
        selected.observation_id if selected is not None else None,
        selected.value if selected is not None else None,
        tuple(sorted(ids)),
        tuple(r.observation_id for r in active),
        tuple(sorted({e for r in alternatives for e in r.evidence_ids})),
        links,
        conflicts,
        tuple(sorted(reasons)),
        frozenset(quality),
        summary,
        policy,
    )
