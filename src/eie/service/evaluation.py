"""Three descriptive evaluation axes; infrastructure is not predictive alpha."""

from eie.service.models import EvaluationDimension, EvaluationResult
from eie.service.repository import ServiceRepository
from eie.service.schema import ComponentKind as C
from eie.service.schema import EvaluationAxis as A
from eie.service.schema import ValidationDepth
from eie.service.validation import validate


def evaluate(repository: ServiceRepository, snapshot_id: str) -> EvaluationResult:
    snapshot = repository.snapshot(snapshot_id)
    if snapshot is None:
        from eie.service.errors import ServiceError
        from eie.service.schema import ServiceCode

        raise ServiceError(ServiceCode.NOT_FOUND)
    validation = validate(repository, snapshot_id, ValidationDepth.DEEP)
    components = {c.kind: c for c in snapshot.components}

    def axis(kind: A, selected: tuple[C, ...]) -> EvaluationDimension:
        checks = tuple(
            (
                k.value,
                components[k].state.value + ":" + components[k].completeness.value
                if k in components
                else "NOT_SELECTED",
            )
            for k in selected
        )
        refs = tuple(
            sorted(
                {r.owner_id for k in selected if k in components for r in components[k].references}
            )
        )
        return EvaluationDimension(kind, checks, refs)

    operational = EvaluationDimension(
        A.OPERATIONAL_CORRECTNESS,
        (
            ("DEEP_REPLAY_VALIDATION", "PASS" if validation.valid else "FAIL"),
            ("HISTORICAL_IMMUTABILITY", "PINNED"),
            ("SOURCE_OUTAGE_SEPARATION", "SUPPORTED"),
        ),
        (snapshot_id,),
    )
    return EvaluationResult(
        snapshot_id,
        (
            operational,
            axis(
                A.INFORMATION_QUALITY,
                (
                    C.IDENTITY,
                    C.EVIDENCE,
                    C.EXTRACTION,
                    C.INTELLIGENCE,
                    C.ANALOGUES,
                    C.INTERACTIONS,
                    C.SOURCE_RELIABILITY,
                    C.SOURCE_COVERAGE,
                ),
            ),
            axis(A.MARKET_IMPACT_USEFULNESS, (C.STUDIES, C.IMPACT, C.REACTION)),
        ),
    )
