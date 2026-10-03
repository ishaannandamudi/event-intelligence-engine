from dataclasses import replace

import pytest

from eie.analogues.responses import extract_response, model_cohort, response_spec, study_cohort
from eie.analogues.schema import ResponseMetric
from eie.core.errors import ValidationError
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.impact.engine import ImpactEngine
from eie.impact.inputs import ImpactInputs
from eie.impact.models import ModelType
from eie.impact.repository import ImpactRepository
from eie.studies.engine import StudyEngine
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketQuote,
    OptionsMarketObservation,
    ReturnType,
)
from eie.studies.repository import StudyRepository
from examples.analogue_fixtures import fixture
from examples.counterfactual_impact import CARS, ESTIMATION
from examples.event_studies import MINUTE, market
from examples.historical_analogues import POLICY


@pytest.fixture
def measured(tmp_path):
    data = fixture(tmp_path, POLICY)
    try:
        row, original = data.rows[-1], data.studies[-1]
        at = original.anchor_ns
        quotes = (
            MarketQuote("security_fixture", at - MINUTE, at - MINUTE, 99, 101, 10, 20, "SYNTHETIC"),
            MarketQuote(
                "security_fixture", at + 5 * MINUTE, at + 5 * MINUTE, 99, 103, 10, 20, "SYNTHETIC"
            ),
        )
        options = tuple(
            OptionsMarketObservation(
                "security_fixture", t, t, "2026-12-01", "1M", "ATM", iv, -0.03, 10, 20, "SYNTHETIC"
            )
            for t, iv in ((at - MINUTE, 0.2), (at + 5 * MINUTE, 0.3))
        )
        supplied = replace(original.market, quotes=quotes, options=options)
        reader = DatasetReader(DatasetRepository(data.store), row.build_id)
        study = StudyEngine(
            reader, IndexedMarketProvider((supplied,)), original.definition.selection_as_of_ns
        ).study_event(
            row.event_id,
            "security_fixture",
            original.definition.anchor,
            original.definition.window,
            data_as_of_ns=original.definition.data_as_of_ns,
            policy=replace(original.definition.policy, options_key=("2026-12-01", "1M", "ATM")),
        )
        StudyRepository(data.store).append(study)
        impact = ImpactEngine().impact_event(
            ImpactInputs(study, study.market, market(instrument="market_fixture")),
            ModelType.MARKET_ADJUSTED,
            ESTIMATION,
            CARS,
        )
        ImpactRepository(data.store).append(impact)
        yield data, row, study, impact
    finally:
        data.store.close()


@pytest.mark.parametrize("metric", list(ResponseMetric))
def test_every_supported_metric_reuses_real_study_or_impact(measured, metric):
    data, row, study, impact = measured
    is_impact = metric in (
        ResponseMetric.ABNORMAL_RETURN,
        ResponseMetric.ABS_ABNORMAL_RETURN,
        ResponseMetric.CAR,
    )
    spec = response_spec(
        study,
        metric,
        impact=impact if is_impact else None,
        car_label="post" if metric is ResponseMetric.CAR else None,
    )
    r = data.repository.index_response(
        row.row_id, study.study_id, spec, impact.impact_id if is_impact else None
    )
    assert r.value is not None and r.spec.metric is metric
    expected = {
        ResponseMetric.RAW_RETURN: study.curve[-1].value,
        ResponseMetric.ABS_RAW_RETURN: abs(study.curve[-1].value),
        ResponseMetric.ABNORMAL_RETURN: impact.abnormal_curve[-1].abnormal_return,
        ResponseMetric.ABS_ABNORMAL_RETURN: abs(impact.abnormal_curve[-1].abnormal_return),
        ResponseMetric.CAR: impact.cars[0].value,
        ResponseMetric.REALIZED_VOL_CHANGE: study.volatility.difference,
        ResponseMetric.RELATIVE_VOLUME: study.volume[-1].relative,
        ResponseMetric.IV_CHANGE: 0.1,
        ResponseMetric.SPREAD_CHANGE: 2.0,
    }[metric]
    assert r.value == pytest.approx(expected)
    assert r.completed_ns == study.post_end_ns and r.available_ns >= r.completed_ns
    assert data.repository.response(r.response_id) == r
    if is_impact:
        assert r.impact_id == impact.impact_id and r.model_id == impact.model_spec.model_id
    else:
        assert r.impact_id is None


@pytest.mark.parametrize(
    "kind",
    ["event", "revision", "dataset", "cohort", "offset", "window", "model", "absent_impact", "car"],
)
def test_incompatible_response_contracts_never_mix(measured, kind):
    data, row, study, impact = measured
    spec = response_spec(study)
    chosen_impact = None
    if kind == "event":
        row = data.rows[0]
    elif kind == "revision":
        row = replace(row, revision=2)
    elif kind == "dataset":
        row = replace(row, dataset_fingerprint="b" * 64)
    elif kind == "cohort":
        spec = replace(spec, study_cohort="b" * 64)
    elif kind == "offset":
        spec = replace(spec, offset_ns=study.definition.window.post_ns + 1)
    elif kind == "window":
        spec = response_spec(study, ResponseMetric.IV_CHANGE, offset_ns=MINUTE)
    elif kind in ("model", "absent_impact"):
        spec = response_spec(study, ResponseMetric.ABNORMAL_RETURN, impact=impact)
        if kind == "model":
            chosen_impact = ImpactEngine().impact_event(
                impact.inputs, ModelType.SECTOR_ADJUSTED, ESTIMATION, CARS
            )
    else:
        spec = response_spec(study, ResponseMetric.CAR, impact=impact, car_label="absent")
        chosen_impact = impact
    with pytest.raises(ValidationError):
        extract_response(row, study, spec, chosen_impact)


def test_raw_and_abnormal_car_model_cohorts_remain_distinct(measured):
    _, _, study, market_impact = measured
    sector = ImpactEngine().impact_event(
        market_impact.inputs, ModelType.SECTOR_ADJUSTED, ESTIMATION, CARS
    )
    assert model_cohort(market_impact) != model_cohort(sector)
    assert response_spec(study) != response_spec(
        study, ResponseMetric.ABNORMAL_RETURN, impact=market_impact
    )
    assert response_spec(
        study, ResponseMetric.CAR, impact=market_impact, car_label="post"
    ) != response_spec(study, ResponseMetric.CAR, impact=sector, car_label="post")
    assert study_cohort(study) != study_cohort(
        replace(
            study,
            definition=replace(
                study.definition, policy=replace(study.definition.policy, minimum_baseline_bars=4)
            ),
        )
    )
    assert study_cohort(study) != study_cohort(
        replace(
            study,
            market=replace(
                study.market,
                identity=replace(
                    study.market.identity,
                    return_type=ReturnType.TOTAL_RETURN,
                    adjustment=Adjustment.SPLIT_DIVIDEND_ADJUSTED,
                ),
            ),
        )
    )


@pytest.mark.parametrize("metric", [ResponseMetric.IV_CHANGE, ResponseMetric.SPREAD_CHANGE])
def test_missing_supplied_metrics_are_typed_unavailable(tmp_path, metric):
    data = fixture(tmp_path, POLICY)
    try:
        row, study = data.rows[-1], data.studies[-1]
        r = data.repository.index_response(row.row_id, study.study_id, response_spec(study, metric))
        assert r.value is None and r.reason == "METRIC_NOT_AVAILABLE"
    finally:
        data.store.close()


def test_source_response_fingerprints_and_exact_model_specs_checked(measured):
    data, row, study, impact = measured
    with pytest.raises(ValidationError):
        data.repository.index_response(row.row_id, "absent", response_spec(study))
    with pytest.raises(ValidationError):
        data.repository.index_response(
            row.row_id,
            study.study_id,
            response_spec(study, ResponseMetric.ABNORMAL_RETURN, impact=impact),
            "absent",
        )
    # Missing/altered source records cannot validate a retained derived response.
    r = data.repository.index_response(row.row_id, study.study_id, response_spec(study))
    data.store._connection.execute("DROP TRIGGER immutable_study_results_update")
    data.store._connection.execute(
        "UPDATE study_results SET fingerprint='bad' WHERE id=?", (study.study_id,)
    )
    from eie.core.errors import PersistenceError

    with pytest.raises(PersistenceError):
        data.repository.response(r.response_id)
