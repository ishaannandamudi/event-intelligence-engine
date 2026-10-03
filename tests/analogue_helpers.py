"""Small transparent mathematical inputs, independent of persistence fixtures."""

from dataclasses import replace

from eie.analogues.models import (
    AnalogueFeature,
    AnaloguePolicy,
    AnalogueQuery,
    ContextFeature,
    FeatureRow,
    FeatureValue,
    ResponseObservation,
    ResponseSpec,
)
from eie.analogues.pool import PoolInputs
from eie.analogues.schema import FeatureKind, FeatureSource, ResponseMetric, SectorCondition

FP = "a" * 64
SPEC = ResponseSpec(ResponseMetric.RAW_RETURN, FP, 1)
TYPE = AnalogueFeature("type", FeatureKind.CATEGORICAL, FeatureSource.EVENT_TYPE, "type")
NUMERIC = AnalogueFeature("number", FeatureKind.NUMERIC, FeatureSource.CONTEXT, "number")
POLICY = AnaloguePolicy(
    features=(NUMERIC, TYPE),
    sector_condition=SectorCondition.UNCONDITIONED,
    minimum_history=5,
    minimum_scale_samples=5,
    maximum_distance=20,
)


def row(
    i=1,
    *,
    value=1.0,
    type_value="EARNINGS.RELEASE",
    cutoff=None,
    policy=POLICY,
    contexts=(),
    **kwargs,
):
    known = i * 10 if cutoff is None else cutoff
    values = tuple(
        FeatureValue(
            f.name,
            type_value if f.name == "type" else value,
            f.source.value + ":" + f.selector,
            ("input",),
            "MISSING" if (type_value if f.name == "type" else value) is None else None,
        )
        for f in policy.features
    )
    base = FeatureRow(
        "evt_" + str(i).zfill(64),
        1,
        "revision" + str(i),
        FP,
        "dataset",
        FP,
        None,
        "EARNINGS.RELEASE",
        "EARNINGS",
        ("issuer",),
        ("evt_" + str(i).zfill(64),),
        known,
        known,
        known,
        "intelligence" + str(i),
        FP,
        policy.policy_id,
        values,
        contexts,
        None,
        None,
        "MODERATE",
        (),
        None,
        (),
        (),
        (),
    )
    return replace(base, **kwargs)


def response(feature, value=0.1, *, spec=SPEC, **kwargs):
    return replace(
        ResponseObservation(
            feature.row_id,
            feature.event_id,
            feature.revision,
            spec,
            "study" + feature.event_id,
            FP,
            None,
            None,
            None,
            value,
            feature.event_available_ns + 1,
            feature.event_available_ns + 1,
            False,
            (),
            (),
            "MISSING" if value is None else None,
        ),
        **kwargs,
    )


def inputs(*, count=7, target_value=4.0, values=None, policy=POLICY, response_values=None):
    target = row(100, value=target_value, policy=policy)
    rows = tuple(
        row(i + 1, value=float(i + 1) if values is None else values[i], policy=policy)
        for i in range(count)
    )
    responses = tuple(
        response(r, float(i - 3) / 100 if response_values is None else response_values[i])
        for i, r in enumerate(rows)
    )
    query = AnalogueQuery(
        target.row_id,
        target.event_id,
        1,
        target.dataset_id,
        target.feature_cutoff_ns,
        target.available_ns,
        policy.policy_id,
        SPEC,
    )
    return PoolInputs(target, rows, responses, (target.event_id,), count), query


def context(name="number", value=1.0, cutoff=10, **kwargs):
    return ContextFeature(name, value, "SUPPLIED", "1", cutoff, cutoff, **kwargs)
