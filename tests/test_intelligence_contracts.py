"""Immutable typed artifacts reject malformed states, clocks, bounds, and collections."""

from dataclasses import fields, replace

import pytest

from eie.core.errors import ValidationError
from eie.intelligence.models import (
    IntelligenceAudit,
    IntelligenceRunManifest,
    IntelligenceValidation,
    Metric,
    NoveltyComponent,
    UncertaintyComponent,
)
from eie.intelligence.policy import (
    ImportancePolicy,
    IntelligenceLimits,
    IntelligencePolicy,
    MaterialityPolicy,
    NoveltyPolicy,
)
from eie.intelligence.schema import (
    Importance,
    IntelligenceMode,
    NoveltyDimension,
    NoveltyState,
    UncertaintyKind,
    UncertaintyState,
    ValidationState,
)
from eie.persistence.codec import dumps, loads
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import batch, source
from tests.intelligence_helpers import compute, ingest


def artifacts():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        r = compute(store, b)
        return (
            r,
            r.manifest,
            r.manifest.request,
            r.manifest.current[0],
            r.novelty[0],
            r.novelty[0].metrics[0],
            r.materiality,
            r.materiality.components[0],
            r.static_importance,
            r.uncertainty[0],
            IntelligencePolicy(),
            NoveltyPolicy(),
            ImportancePolicy(),
            MaterialityPolicy(),
            IntelligenceLimits(),
            IntelligenceAudit(
                r.intelligence_id, ("impact_test",), ("a" * 64,), r.uncertainty[4], 20
            ),
            IntelligenceRunManifest(
                (r.intelligence_id,),
                (r.result_fingerprint,),
                r.manifest.request.dataset_id,
                r.manifest.request.policy_id,
                20,
            ),
            IntelligenceValidation(r.intelligence_id, ValidationState.WARN, ("KNOWN_LIMITATION",)),
        )


ARTIFACTS = artifacts()


@pytest.mark.parametrize("value,field", [(v, f.name) for v in ARTIFACTS for f in fields(v)])
def test_every_public_field_requires_its_immutable_declared_type(value, field):
    with pytest.raises(ValidationError):
        replace(value, **{field: object()})


@pytest.mark.parametrize("value", ARTIFACTS)
def test_allowlisted_codec_roundtrip(value):
    assert dumps(loads(dumps(value))) == dumps(value)


@pytest.mark.parametrize(
    "options",
    [
        {"historical_samples": 0},
        {"historical_samples": 10001},
        {"minimum_numeric_samples": 257},
        {"near_zero": 0},
        {"near_zero": float("nan")},
        {"near_zero": True},
    ],
)
def test_invalid_numeric_policies(options):
    with pytest.raises(ValidationError):
        NoveltyPolicy(**options)


@pytest.mark.parametrize(
    "options",
    [
        {"families": ()},
        {"event_types": (("B", Importance.LOW), ("A", Importance.HIGH))},
    ],
)
def test_importance_policy_requires_complete_taxonomy_and_sorted_overrides(options):
    with pytest.raises(ValidationError):
        ImportancePolicy(**options)


@pytest.mark.parametrize(
    "options",
    [
        {"ratio_thresholds": (0, 1, 2)},
        {"ratio_thresholds": (2, 1, 3)},
        {"scale_measurements": ()},
        {"combination_rule": "MAGIC_SCORE"},
        {"currency_thresholds": (("USD", (0, 1, 2)),)},
    ],
)
def test_materiality_policy_rejects_ambiguous_or_opaque_rules(options):
    with pytest.raises(ValidationError):
        MaterialityPolicy(**options)


@pytest.mark.parametrize("name", [f.name for f in fields(IntelligenceLimits)])
@pytest.mark.parametrize("value", [0, True, 100001])
def test_all_operational_bounds_are_enforced(name, value):
    with pytest.raises(ValidationError):
        IntelligenceLimits(**{name: value})


def test_metric_missingness_and_dimension_state_are_not_interchangeable():
    with pytest.raises(ValidationError):
        Metric("value", None)
    with pytest.raises(ValidationError):
        Metric("value", 0, reason="MISSING")
    with pytest.raises(ValidationError):
        NoveltyComponent(NoveltyDimension.DOCUMENT, "document", NoveltyState.INITIATED, (), ())
    with pytest.raises(ValidationError):
        NoveltyComponent(NoveltyDimension.NUMERIC, "claim", NoveltyState.COMPARABLE, ("z", "a"), ())
    with pytest.raises(ValidationError):
        NoveltyComponent(
            NoveltyDimension.NUMERIC,
            "claim",
            NoveltyState.COMPARABLE,
            (),
            (),
            (Metric("b", 1), Metric("a", 2)),
        )
    with pytest.raises(ValidationError):
        UncertaintyComponent(
            UncertaintyKind.SOURCE, UncertaintyState.READY_FOR_ANALOGUE_SEARCH, ("BAD_COMPONENT",)
        )
    with pytest.raises(ValidationError):
        UncertaintyComponent(UncertaintyKind.ANALOGUE, UncertaintyState.LOW, ("NO_PROBABILITY",))


def test_result_membership_cutoffs_modes_and_schema_are_validated():
    result = ARTIFACTS[0]
    manifest = result.manifest
    req = manifest.request
    with pytest.raises(ValidationError):
        replace(req, available_ns=19)
    with pytest.raises(ValidationError):
        replace(req, revision_number=0)
    with pytest.raises(ValidationError):
        replace(manifest, schema_version="2.0")
    with pytest.raises(ValidationError):
        replace(manifest, excluded_event_ids=())
    with pytest.raises(ValidationError):
        replace(manifest, prior=(manifest.current[0],))
    with pytest.raises(ValidationError):
        replace(manifest, current=(replace(manifest.current[0], known_ns=21),))
    with pytest.raises(ValidationError):
        replace(result, uncertainty=result.uncertainty[:-1])
    with pytest.raises(ValidationError):
        replace(result, mode=IntelligenceMode.EX_POST_AUDIT)
    with pytest.raises(ValidationError):
        replace(result, novelty=(result.novelty[0],) * 2)
    with pytest.raises(ValidationError):
        replace(result.materiality, components=(result.materiality.components[0],) * 2)
    with pytest.raises(ValidationError):
        replace(ARTIFACTS[-2], result_fingerprints=())
    with pytest.raises(ValidationError):
        replace(ARTIFACTS[-3], impact_fingerprints=())
