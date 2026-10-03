"""Structured domain progression is separate from information correction/retraction."""

from eie.core.errors import ValidationError
from eie.events.lifecycle import LifecycleState, validate_transition
from eie.events.revisions import RevisionKind
from eie.events.taxonomy import EventFamily
from eie.interactions.models import EventStateTransition, InteractionEvent
from eie.interactions.schema import DomainState as S
from eie.interactions.schema import EventInteractionRelation as R

DOMAIN_RULES = {
    EventFamily.CORPORATE_ACTION: {
        (S.RUMORED, S.ANNOUNCED),
        (S.ANNOUNCED, S.APPROVED),
        (S.APPROVED, S.CLOSED),
    },
    EventFamily.EARNINGS: {
        (S.ISSUED, S.REVISED),
        (S.REVISED, S.REVISED),
        (S.ISSUED, S.WITHDRAWN),
        (S.REVISED, S.WITHDRAWN),
    },
    EventFamily.MANAGEMENT_GOVERNANCE: {
        (S.DEPARTURE_ANNOUNCED, S.DEPARTURE_EFFECTIVE),
        (S.DEPARTURE_EFFECTIVE, S.REPLACEMENT),
    },
}


def transitions(rows: tuple[InteractionEvent, ...]) -> tuple[EventStateTransition, ...]:
    groups: dict[str, list[InteractionEvent]] = {}
    for row in rows:
        groups.setdefault(row.thread_id, []).append(row)
    result = []
    for thread, values in sorted(groups.items()):
        layers: dict[int, list[InteractionEvent]] = {}
        for row in values:
            layers.setdefault(row.known_ns, []).append(row)
        clocks = sorted(layers)
        for earlier, later in zip(clocks, clocks[1:], strict=False):
            if len(layers[earlier]) != 1 or len(layers[later]) != 1:
                continue
            a, b = layers[earlier][0], layers[later][0]
            if b.retracted:
                relation = R.RETRACTION_OF
            elif b.revision_kind is RevisionKind.CORRECTION:
                relation = R.CORRECTION_OF
            elif a.state == b.state:
                continue
            else:
                if a.event_type.family is not b.event_type.family:
                    raise ValidationError("lifecycle thread cannot mix domain families")
                if isinstance(a.state, LifecycleState) and isinstance(b.state, LifecycleState):
                    validate_transition(a.state, b.state)
                elif (a.state, b.state) not in DOMAIN_RULES.get(b.event_type.family, set()):
                    raise ValidationError("unsupported domain lifecycle progression")
                relation = R.STATE_TRANSITION_TO
            result.append(
                EventStateTransition(
                    thread,
                    a.artifact_id,
                    b.artifact_id,
                    a.state,
                    b.state,
                    b.known_ns,
                    b.occurrence,
                    relation,
                )
            )
    return tuple(sorted(result, key=lambda r: r.artifact_id))
