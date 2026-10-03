"""Behavior follows explicit owner actions, never value differences."""

from eie.events.revisions import RevisionKind
from eie.evidence.schema import ClaimStatus
from eie.reliability.schema import RevisionBehavior as B


def claim_behavior(status: ClaimStatus, *, expected: bool = False) -> B:
    return {
        ClaimStatus.ASSERTED: B.EXPECTED_REVISION if expected else B.ORIGINAL,
        ClaimStatus.CONFIRMED: B.EXPECTED_REVISION if expected else B.ORIGINAL,
        ClaimStatus.UPDATED: B.EXPECTED_REVISION if expected else B.UPDATE,
        ClaimStatus.CORRECTED: B.ERROR_CORRECTION,
        ClaimStatus.RETRACTED: B.RETRACTION,
        ClaimStatus.WITHDRAWN: B.RETRACTION,
    }[status]


def event_behavior(kind: RevisionKind, *, macro: bool) -> B:
    return {
        RevisionKind.ORIGINAL: B.ORIGINAL,
        RevisionKind.UPDATE: B.UPDATE,
        RevisionKind.CORRECTION: B.ERROR_CORRECTION,
        RevisionKind.REVISION: B.EXPECTED_REVISION if macro else B.UPDATE,
        RevisionKind.SUPERSEDED: B.SUPERSESSION,
        RevisionKind.CANCELLATION: B.UPDATE,
        RevisionKind.RESCHEDULE: B.SCHEDULE_CHANGE,
    }[kind]
