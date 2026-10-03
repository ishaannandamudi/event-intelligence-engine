"""Freeze prior analogues, attach a later real response, and prove identical restart."""

from pathlib import Path
from tempfile import TemporaryDirectory

from eie.analogues.engine import AnalogueEngine
from eie.analogues.models import AnalogueFeature, AnaloguePolicy, AnalogueQuery, AnalogueRunManifest
from eie.analogues.repository import AnalogueRepository
from eie.analogues.schema import FeatureKind, FeatureSource
from eie.analogues.validation import validate
from eie.core.fingerprints import fingerprint
from eie.corporate.types import EarningsPeriod
from eie.expectations.consensus import consensus_snapshot
from eie.expectations.models import REVENUE, MeasurementContext
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.surprise import calculate_surprise
from eie.impact.engine import ImpactEngine
from eie.impact.inputs import ImpactInputs
from eie.impact.models import ModelType
from eie.impact.repository import ImpactRepository
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.sources.trust import SourcePrecedencePolicy
from examples.analogue_fixtures import fixture
from examples.counterfactual_impact import CARS, ESTIMATION
from examples.event_studies import market

POLICY = AnaloguePolicy(
    features=(
        AnalogueFeature("event_type", FeatureKind.CATEGORICAL, FeatureSource.EVENT_TYPE, "type"),
        AnalogueFeature("volatility", FeatureKind.NUMERIC, FeatureSource.CONTEXT, "volatility"),
    )
)


def demonstrate(root: Path) -> dict[str, object]:
    data = fixture(root, POLICY)
    repo, target, study = data.repository, data.rows[-1], data.studies[-1]
    try:
        query = AnalogueQuery(
            target.row_id,
            target.event_id,
            target.revision,
            target.dataset_id,
            target.feature_cutoff_ns,
            target.available_ns,
            POLICY.policy_id,
            data.spec,
        )
        frozen = AnalogueEngine(repo).analogues(query)
        before = dumps(frozen)
        response = repo.index_response(target.row_id, study.study_id, data.spec)
        reaction = repo.attach_reaction(
            frozen.result_id, response.response_id, target.available_ns + 1
        )
        # A real EIE-9 result remains an independent counterfactual object.
        benchmark = market(instrument="market_fixture")
        impact = ImpactEngine().impact_event(
            ImpactInputs(study, study.market, benchmark),
            ModelType.MARKET_ADJUSTED,
            ESTIMATION,
            CARS,
        )
        ImpactRepository(data.store).append(impact)
        # Metadata-only fixtures have no numeric actual/consensus. EIE-5 retains missingness.
        settings = ExpectationsSettings()
        context = MeasurementContext(
            target.event_id, REVENUE, EarningsPeriod(2026, 3, "2026-09-30")
        )
        fp = fingerprint("eie:analogue-example-expectations:v1", settings)
        consensus = consensus_snapshot(
            context, (), target.feature_cutoff_ns - 1, settings, SourcePrecedencePolicy(), fp
        )
        information = calculate_surprise(
            context, target.feature_cutoff_ns, None, consensus, settings, fp
        )
        run = AnalogueRunManifest(
            (frozen.result_id,), (frozen.fingerprint,), target.dataset_id, target.available_ns
        )
        repo.append_run(run)
        assert validate(repo, frozen.result_id, deep=True).state.value != "INVALID"
        assert dumps(repo.get(frozen.result_id)) == before
        reaction_bytes = dumps(reaction)
    finally:
        data.store.close()
    with SQLiteEventStore(root / "studies.sqlite") as store:
        restarted = AnalogueRepository(store)
        assert dumps(restarted.get(frozen.result_id)) == before
        assert dumps(restarted.reaction(reaction.reaction_id)) == reaction_bytes
        assert restarted.run(run.run_id) == run
    return {
        "selected": [
            (c.event_id, c.total_distance, [(d.feature, d.distance, d.rule) for d in c.components])
            for c in frozen.candidates
        ],
        "prior_count": frozen.distribution.count,
        "historical_median": frozen.distribution.median,
        "signed_reaction_percentile": reaction.signed_percentile,
        "magnitude_reaction_percentile": reaction.magnitude_percentile,
        "reaction_residual": reaction.signed_residual,
        "information_surprise": {
            "raw": information.raw_surprise,
            "reasons": information.reason_codes,
        },
        "counterfactual_impact_id": impact.impact_id,
        "counterfactual_abnormal_returns": [p.abnormal_return for p in impact.abnormal_curve],
        "source_cutoff_ns": query.known_as_of_ns,
        "actual_available_ns": query.available_ns,
        "reaction_available_ns": reaction.available_ns,
        "frozen_members_unchanged": True,
        "restart_byte_identical": True,
    }


if __name__ == "__main__":
    import json

    with TemporaryDirectory() as directory:
        print(json.dumps(demonstrate(Path(directory)), sort_keys=True))
