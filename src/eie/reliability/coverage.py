"""Coverage is conditional on a pinned expected universe and explicit evidence states."""

from eie.core.errors import ValidationError
from eie.reliability.models import CoverageAssessment, CoverageUniverse, SourceCoverageResult
from eie.reliability.schema import CoverageState as S
from eie.reliability.schema import ReliabilityQuality as Q


def calculate(
    universe: CoverageUniverse | None, assessments: tuple[CoverageAssessment, ...], cutoff: int
) -> SourceCoverageResult:
    if universe is None:
        if assessments:
            raise ValidationError("coverage assessments need a pinned denominator")
        return SourceCoverageResult(
            None, cutoff, None, 0, 0, 0, 0, 0, None, (), frozenset({Q.INCOMPLETE_UNIVERSE})
        )
    if universe.known_ns >= cutoff:
        raise ValidationError("coverage denominator must be strictly prior")
    keys = {s.key for s in universe.slots}
    latest: dict[str, CoverageAssessment] = {}
    for assessment in assessments:
        if assessment.universe_id != universe.artifact_id or assessment.slot_key not in keys:
            raise ValidationError("coverage membership does not belong to denominator")
        if assessment.known_ns >= cutoff:
            continue
        old = latest.get(assessment.slot_key)
        if old is not None and old.known_ns == assessment.known_ns and old != assessment:
            raise ValidationError("same-time conflicting coverage assessment")
        if old is None or assessment.known_ns > old.known_ns:
            latest[assessment.slot_key] = assessment
    counts = dict.fromkeys(S, 0)
    for slot in universe.slots:
        # Future expectations are not evaluated as missing/empty/observed.
        if slot.due_ns >= cutoff:
            counts[S.UNKNOWN] += 1
        else:
            counts[latest[slot.key].state if slot.key in latest else S.UNKNOWN] += 1
    denominator = len(keys) - counts[S.NOT_APPLICABLE]
    return SourceCoverageResult(
        universe.artifact_id,
        cutoff,
        len(keys),
        counts[S.OBSERVED],
        counts[S.MISSING],
        counts[S.KNOWN_EMPTY],
        counts[S.UNKNOWN],
        counts[S.NOT_APPLICABLE],
        counts[S.OBSERVED] / denominator if denominator else None,
        tuple(sorted(a.artifact_id for a in latest.values())),
        frozenset({Q.INCOMPLETE_UNIVERSE})
        if not universe.complete or counts[S.UNKNOWN]
        else frozenset(),
    )
