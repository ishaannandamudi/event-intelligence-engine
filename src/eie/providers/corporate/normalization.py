"""Deterministic claim selection and canonical corporate event conversion."""

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState, QualityFlag
from eie.corporate.models import (
    CorporateClaim,
    CorporateEvent,
    CorporateIdentity,
    CorporateKind,
    CorporateStatus,
    Dividend,
    EarningsCall,
    EarningsRelease,
    EarningsSchedule,
    Split,
    SymbolChange,
)
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.persistence.memory import InMemoryEventStore
from eie.sources.trust import SourceClass


def event_type_for(kind: CorporateKind) -> EventType:
    if kind in (
        CorporateKind.EARNINGS_SCHEDULE,
        CorporateKind.EARNINGS_RELEASE,
        CorporateKind.EARNINGS_CALL,
    ):
        return EventType(EventFamily.EARNINGS, f"EARNINGS.{kind.value.removeprefix('EARNINGS_')}")
    if kind in (
        CorporateKind.CEO_DEPARTURE,
        CorporateKind.CEO_APPOINTMENT,
        CorporateKind.CFO_DEPARTURE,
        CorporateKind.CFO_APPOINTMENT,
        CorporateKind.BOARD_CHANGE,
        CorporateKind.AUDITOR_CHANGE,
    ):
        return EventType(EventFamily.MANAGEMENT_GOVERNANCE, f"MANAGEMENT.{kind.value}")
    return EventType(EventFamily.CORPORATE_ACTION, f"CORPORATE_ACTION.{kind.value}")


def canonical_identity(identity: CorporateIdentity) -> EventIdentity:
    scope = EventScope(
        ScopeKind.SECURITY if identity.security_id is not None else ScopeKind.COMPANY,
        (identity.security_id if identity.security_id is not None else identity.issuer_id,),
    )
    return EventIdentity(event_type_for(identity.kind), scope, identity.corporate_id)


def _schedule_ns(claim: CorporateClaim) -> int | None:
    payload = claim.payload
    return (
        payload.scheduled_for_ns if isinstance(payload, (EarningsSchedule, EarningsCall)) else None
    )


def _lifecycle(
    kind: CorporateKind,
    selected: CorporateClaim,
    previous: CorporateEvent | None,
    revision_kind: RevisionKind,
) -> LifecycleState:
    if revision_kind is RevisionKind.CANCELLATION:
        return LifecycleState.CANCELLED
    if revision_kind is RevisionKind.RESCHEDULE:
        return LifecycleState.RESCHEDULED
    if kind in (CorporateKind.EARNINGS_SCHEDULE, CorporateKind.EARNINGS_CALL):
        return LifecycleState.SCHEDULED
    return LifecycleState.RELEASED if previous is None else LifecycleState.UPDATED


def _facts(event: CorporateEvent) -> tuple[StructuredFact, ...]:
    selected = event.selected_claim
    payload = selected.payload
    values: dict[str, str | int | bool] = {
        "corporate_id": event.corporate_id,
        "kind": event.identity.kind.value,
        "payload_fingerprint": fingerprint("eie:corporate-payload:v1", payload),
        "provider_event_id": selected.provider_event_id,
        "provider_id": selected.provider_id,
        "status": selected.status.value,
    }
    if isinstance(payload, (EarningsSchedule, EarningsCall, EarningsRelease)):
        values["reference_period"] = payload.period.key
    if selected.announced_date is not None:
        values["announced_date"] = selected.announced_date
    if isinstance(payload, EarningsSchedule):
        values["release_timing"] = payload.release_timing.value
        values["schedule_precision"] = payload.precision.value
        if payload.scheduled_date is not None:
            values["scheduled_date"] = payload.scheduled_date
    if isinstance(payload, Dividend) and payload.amount is not None:
        values["amount_micros"] = payload.amount.amount_micros
        values["currency"] = payload.amount.currency
        values["per_share"] = True
    if isinstance(payload, Split):
        values["ratio_numerator"] = payload.numerator
        values["ratio_denominator"] = payload.denominator
    if isinstance(payload, SymbolChange):
        values["old_symbol"] = f"{payload.old_ticker}:{payload.old_exchange}"
        values["new_symbol"] = f"{payload.new_ticker}:{payload.new_exchange}"
    return tuple(StructuredFact(name, values[name]) for name in sorted(values))


def normalize_corporate(
    identity: CorporateIdentity,
    additions: tuple[CorporateClaim, ...],
    previous: CorporateEvent | None,
    store: InMemoryEventStore,
    settings: Settings,
    ingested_ns: int,
) -> tuple[CorporateEvent, CanonicalEvent] | None:
    by_key = {} if previous is None else {claim.key: claim for claim in previous.claims}
    changed_keys: set[tuple[str, str]] = set()
    for claim in additions:
        old = by_key.get(claim.key)
        if old is not None and old.observation_id == claim.observation_id:
            continue
        if old is not None and claim.available_from_ns <= old.available_from_ns:
            raise PersistenceError("corporate claim knowledge must advance")
        by_key[claim.key] = claim
        changed_keys.add(claim.key)
    if not changed_keys:
        return None
    claims = tuple(by_key[key] for key in sorted(by_key))
    order = settings.source_precedence.order_for(f"corporate.{identity.kind.value.lower()}")
    best_rank = min(order.index(claim.source_class) for claim in claims)
    leaders = tuple(claim for claim in claims if order.index(claim.source_class) == best_rank)
    previous_key = None if previous is None else previous.selected_claim_key
    selected = next((claim for claim in leaders if claim.key == previous_key), leaders[0])
    competing = {
        (
            claim.payload,
            claim.status,
            claim.announced_at_ns,
            claim.effective_at_ns,
            claim.announced_date,
        )
        for claim in claims
    }
    conflict = len(competing) > 1
    top_conflict = (
        len(
            {
                (
                    claim.payload,
                    claim.status,
                    claim.announced_at_ns,
                    claim.effective_at_ns,
                    claim.announced_date,
                )
                for claim in leaders
            }
        )
        > 1
    )
    flags = frozenset(
        ({QualityFlag.CONFLICTING_SOURCES} if conflict else set())
        | (
            {QualityFlag.CORRECTED}
            if previous is not None
            and any(key in {item.key for item in previous.claims} for key in changed_keys)
            else set()
        )
        | (
            {QualityFlag.ENTITY_UNRESOLVED}
            if getattr(selected.payload, "unresolved_party_name", None) is not None
            else set()
        )
        | set().union(*(claim.quality_flags for claim in claims))
    )
    reasons = tuple(
        sorted(
            ({"DISAGREEING_PROVIDER_CLAIMS"} if conflict else set())
            | ({"EQUAL_RANK_DISAGREEMENT"} if top_conflict else set())
            | ({"SOURCE_PRECEDENCE"} if conflict and not top_conflict else set())
        )
    )
    if conflict:
        confirmation = ConfirmationState.DISPUTED
    elif selected.source_class in (
        SourceClass.OFFICIAL_PRIMARY,
        SourceClass.EXCHANGE_PRIMARY,
        SourceClass.COMPANY_PRIMARY,
        SourceClass.REGULATED_FILING,
    ):
        confirmation = ConfirmationState.CONFIRMED_PRIMARY
    elif len(claims) > 1:
        confirmation = ConfirmationState.CORROBORATED
    else:
        confirmation = ConfirmationState.REPORTED
    if previous is None:
        kind = RevisionKind.ORIGINAL
    elif (
        selected.status is CorporateStatus.CANCELLED
        and previous.selected_claim.status is not CorporateStatus.CANCELLED
    ):
        kind = RevisionKind.CANCELLATION
    elif (
        _schedule_ns(previous.selected_claim) is not None
        and _schedule_ns(selected) is not None
        and _schedule_ns(previous.selected_claim) != _schedule_ns(selected)
        and identity.kind in (CorporateKind.EARNINGS_SCHEDULE, CorporateKind.EARNINGS_CALL)
    ):
        kind = RevisionKind.RESCHEDULE
    elif previous.selected_claim_key in changed_keys:
        kind = RevisionKind.CORRECTION
    else:
        kind = RevisionKind.UPDATE
    if previous is not None and previous.selected_claim.status is CorporateStatus.CANCELLED:
        raise ValidationError("cancelled corporate event cannot be revised")
    revision = EventRevision(
        1 if previous is None else previous.revision.number + 1,
        kind,
        ingested_ns,
        None if previous is None else previous.revision.number,
        reason_codes=() if previous is None else ("CORPORATE_CLAIM_UPDATE",),
    )
    corporate = CorporateEvent(
        identity,
        revision,
        claims,
        selected.key,
        confirmation,
        flags,
        reasons,
    )
    raw = store.get_observation(selected.observation_id)
    if raw is None:
        raise PersistenceError("selected corporate claim lacks raw evidence")
    evidence_ids = tuple(sorted({claim.observation_id for claim in claims}))
    source_classes = frozenset(claim.source_class for claim in claims)
    if not source_classes:
        raise ValidationError("corporate source class required")
    canonical = CanonicalEvent(
        canonical_identity(identity),
        revision,
        _lifecycle(identity.kind, selected, previous, kind),
        confirmation,
        evidence_ids,
        source_classes,
        f"{identity.kind.value.replace('_', ' ').title()} {identity.issuer_id}",
        scheduled_for_ns=_schedule_ns(selected),
        announced_at_ns=selected.announced_at_ns,
        published_at_ns=(
            selected.payload.published_at_ns
            if isinstance(selected.payload, EarningsRelease)
            else raw.published_at_ns
        ),
        source_observed_at_ns=raw.source_observed_at_ns,
        ingested_at_ns=ingested_ns,
        effective_at_ns=selected.effective_at_ns,
        facts=_facts(corporate),
        quality_flags=flags,
    )
    return corporate, canonical
