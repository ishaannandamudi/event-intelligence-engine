"""Exact replay from frozen references; optional deep source/index reconstruction."""

from eie.analogues.features import build_features
from eie.analogues.models import AnalogueValidation
from eie.analogues.pool import collect
from eie.analogues.repository import AnalogueRepository
from eie.analogues.responses import extract_response
from eie.analogues.retrieval import retrieve
from eie.analogues.schema import ValidationState
from eie.core.errors import EIEError
from eie.impact.repository import ImpactRepository
from eie.studies.repository import StudyRepository


def validate(repo: AnalogueRepository, identity: str, *, deep: bool = False) -> AnalogueValidation:
    try:
        with repo.store.transaction():
            result = repo.get(identity)
            if result is None:
                return AnalogueValidation(identity, ValidationState.INVALID, ("RESULT_NOT_FOUND",))
            target = repo.require_feature(result.query.target_row_id)
            rows = tuple(repo.require_feature(m.row_id) for m in result.pool)
            responses = tuple(
                repo.require_response(m.response_id)
                for m in result.pool
                if m.response_id is not None
            )
            policy = repo.policy(result.query.policy_id)
            if deep:
                for row in (target, *rows):
                    expected = build_features(
                        repo,
                        row.intelligence_id,
                        policy,
                        tuple(c for c in row.contexts if c.source != row.graph_id),
                        row.graph_id,
                    )
                    if expected != row:
                        return AnalogueValidation(
                            identity, ValidationState.INVALID, ("FEATURE_RECALCULATION_MISMATCH",)
                        )
                by_id = {r.row_id: r for r in rows}
                for response in responses:
                    study = StudyRepository(repo.store).get(response.study_id)
                    impact = (
                        None
                        if response.impact_id is None
                        else ImpactRepository(repo.store).get(response.impact_id)
                    )
                    if (
                        study is None
                        or extract_response(
                            by_id[response.feature_row_id], study, response.spec, impact
                        )
                        != response
                    ):
                        return AnalogueValidation(
                            identity, ValidationState.INVALID, ("RESPONSE_RECALCULATION_MISMATCH",)
                        )
            data = collect(repo, result.query, policy)
            if (
                data.rows != rows
                or data.responses != responses
                or data.excluded_event_ids != result.excluded_event_ids
                or data.total_prior_events != result.diagnostics.total_prior_events
            ):
                return AnalogueValidation(
                    identity, ValidationState.INVALID, ("POOL_BLOCKING_RECALCULATION_MISMATCH",)
                )
            if retrieve(data, result.query, policy) != result:
                return AnalogueValidation(
                    identity, ValidationState.INVALID, ("ANALOGUE_RECALCULATION_MISMATCH",)
                )
            return AnalogueValidation(
                identity,
                ValidationState.VALID_WITH_WARNINGS if result.quality else ValidationState.VALID,
                result.quality,
            )
    except (EIEError, ValueError, TypeError, KeyError, AssertionError):
        return AnalogueValidation(
            identity, ValidationState.INVALID, ("INVALID_REFERENCE_OR_CALCULATION",)
        )
