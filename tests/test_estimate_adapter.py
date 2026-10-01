"""Offline provider capability, drift, credentials, and atomic failure boundaries."""

import json
from dataclasses import replace

import pytest
from expectations_helpers import ISSUER, adapter, ingest, payload, record, seeded

from eie.config.settings import Settings
from eie.core.errors import EIEError, TemporalConsistencyError, ValidationError
from eie.events.revisions import EventRevision, RevisionKind
from eie.expectations.models import EPS_DILUTED, GUIDANCE_EPS
from eie.expectations.settings import ExpectationsSettings
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.estimates.adapter import EstimateAdapter, EstimateFailure, normalized_observation
from eie.providers.estimates.models import EstimateCapability, RecordKind
from eie.providers.estimates.parser import parse_batch, quantity, url
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "NEXT"},
        {"provider_id": "OTHER"},
        {"items": {}},
        {"source_version": " "},
        {"coverage": {}},
        {"coverage": {"event_ids": [], "measurements": [], "has_vintages": "yes"}},
    ],
)
def test_root_schema_drift_is_explicit(changes):
    data = json.loads(payload())
    data.update(changes)
    with pytest.raises(ProviderSchemaMismatchError):
        parse_batch(json.dumps(data).encode(), "SYNTH_A")


@pytest.mark.parametrize(
    "change",
    [
        {"record_id": ""},
        {"observation_type": "FUTURE"},
        {"reference_period": None},
        {"reference_period": {"type": "OTHER"}},
        {"reference_period": {"type": "EARNINGS", "fiscal_year": True}},
        {"quantity": {"value": "2", "unit": {"kind": "COUNT"}}},
        {"quantity": {"value": True, "unit": {"kind": "COUNT"}}},
        {"quantity": {"value": 2, "unit": {"kind": "UNKNOWN"}}},
        {"contributor_count": 1.2},
        {"vintage_at_ns": "100"},
        {"measurement_id": "EPS"},
        {"seasonal_adjustment": "UNKNOWN"},
    ],
)
def test_record_drift_fails_without_partial_state(change):
    service, (eid,) = seeded()
    before = service.engine.canonical_state_bytes()
    row = record(eid, 2)
    row.update(change)
    diagnostic = service.ingest_payload(adapter(), payload([record(eid, 1), row]), 100, 100)
    assert diagnostic.error_category is EstimateFailure.SCHEMA_MISMATCH
    assert service.engine.canonical_state_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"records": []},
        {"records": {}},
        {"item_id": 1},
        {"source_reference": "http://synthetic.example/item"},
        {"published_at_ns": True},
    ],
)
def test_item_drift(change):
    service, (eid,) = seeded()
    data = json.loads(payload([record(eid, 2)]))
    data["items"][0].update(change)
    before = service.engine.canonical_state_bytes()
    diagnostic = service.ingest_payload(adapter(), json.dumps(data).encode(), 100, 100)
    assert diagnostic.error_category is EstimateFailure.SCHEMA_MISMATCH
    assert service.engine.canonical_state_bytes() == before


@pytest.mark.parametrize(
    "reference",
    [
        "https://user:password@synthetic.example/item",
        "https://synthetic.example/item?api_key=synthetic-secret",
        "https://synthetic.example/item?token=synthetic-secret",
        "https://synthetic.example/item#fragment",
        "https://[malformed/item",
        "file:///private/tmp/item",
        "https:///item",
    ],
)
def test_credential_and_malformed_references_rejected(reference):
    with pytest.raises(ValidationError):
        url(reference)
    assert url("https://synthetic.example/item?period=2026")


@pytest.mark.parametrize("data", [b"{", b"\xff", b"not-json"])
def test_invalid_json(data):
    with pytest.raises(ProviderParseError):
        parse_batch(data, "SYNTH_A")


@pytest.mark.parametrize(
    "arguments",
    [
        ("lower", SourceClass.LICENSED_NEWS, frozenset(EstimateCapability)),
        ("SYNTH_A", "LICENSED_NEWS", frozenset(EstimateCapability)),
        ("SYNTH_A", SourceClass.LICENSED_NEWS, set(EstimateCapability)),
        ("SYNTH_A", SourceClass.LICENSED_NEWS, frozenset({"CONSENSUS"})),
    ],
)
def test_adapter_identity_validation(arguments):
    with pytest.raises(ValidationError):
        EstimateAdapter(*arguments)


def test_declared_capabilities_enforced():
    _, (eid,) = seeded()
    source = adapter(capabilities=frozenset({EstimateCapability.CONSENSUS}))
    assert source.capabilities() == frozenset({EstimateCapability.CONSENSUS})
    with pytest.raises(ProviderSchemaMismatchError, match="capabilities"):
        source.parse(payload([record(eid, 2)]))
    source.parse(payload([record(eid, 2, vintage=None)]))
    with pytest.raises(ProviderSchemaMismatchError, match="coverage"):
        source.parse(
            payload(
                coverage={
                    "event_ids": [eid],
                    "measurements": [EPS_DILUTED.code],
                    "has_vintages": True,
                }
            )
        )
    with pytest.raises(ProviderSchemaMismatchError, match="capabilities"):
        source.parse(payload([record(eid, 2, vintage=None, estimate_type="INDIVIDUAL_ANALYST")]))


def test_preflight_limits_disabled_and_attempt_clock():
    service, (eid,) = seeded(settings=Settings())
    with pytest.raises(ValidationError, match="disabled"):
        service.ingest_payload(adapter(), payload(), 1, 1)
    service, (eid,) = seeded(
        settings=Settings(expectations=ExpectationsSettings(enabled=True, max_payload_bytes=20))
    )
    with pytest.raises(ValidationError, match="payload"):
        service.ingest_payload(adapter(), payload(), 1, 1)
    service, (eid,) = seeded(
        settings=Settings(
            accepted_source_classes=frozenset({SourceClass.OFFICIAL_PRIMARY}),
            expectations=ExpectationsSettings(enabled=True),
        )
    )
    with pytest.raises(ValidationError, match="class"):
        service.ingest_payload(adapter(), payload(), 1, 1)
    service, (eid,) = seeded(
        settings=Settings(expectations=ExpectationsSettings(enabled=True, max_items_per_batch=1))
    )
    data = json.loads(payload([record(eid, 2)]))
    data["items"].append(data["items"][0])
    with pytest.raises(ValidationError, match="batch"):
        service.ingest_payload(adapter(), json.dumps(data).encode(), 100, 100)
    with pytest.raises(TemporalConsistencyError, match="follows"):
        service.ingest_payload(adapter(), payload(), 200, 100)


def test_same_item_duplicates_and_conflicting_record_identity():
    service, (eid,) = seeded()
    row = record(eid, 2)
    result = ingest(service, [row, row], 100)
    assert (result.observations_created, result.duplicates_suppressed) == (1, 1)
    before = service.engine.canonical_state_bytes()
    conflict = service.ingest_payload(
        adapter(), payload([row, record(eid, 3)], item_id="bad"), 200, 200
    )
    assert conflict.error_category is EstimateFailure.NORMALIZATION_ERROR
    assert service.engine.canonical_state_bytes() == before
    changed = record(eid, 2, measurement=GUIDANCE_EPS)
    conflict = service.ingest_payload(adapter(), payload([changed], item_id="identity"), 300, 300)
    assert conflict.error_category is EstimateFailure.NORMALIZATION_ERROR
    assert service.engine.canonical_state_bytes() == before
    assert len(service.diagnostics("SYNTH_A")) == 3
    assert not service.diagnostics("OTHER")


@pytest.mark.parametrize(
    "category,state",
    [
        (EstimateFailure.UNAVAILABLE, SourceHealthState.UNAVAILABLE),
        (EstimateFailure.RATE_LIMITED, SourceHealthState.RATE_LIMITED),
        (EstimateFailure.PARSE_ERROR, SourceHealthState.DEGRADED),
        (EstimateFailure.SCHEMA_MISMATCH, SourceHealthState.SCHEMA_MISMATCH),
        (EstimateFailure.NORMALIZATION_ERROR, SourceHealthState.DEGRADED),
    ],
)
def test_failure_categories_preserve_logical_state(category, state):
    service, _ = seeded()
    before = service.engine.canonical_state_bytes()
    result = service.report_failure("SYNTH_A", 100, category)
    assert result.health.state is state
    assert service.engine.health_as_of("SYNTH_A", 99).state is SourceHealthState.UNKNOWN
    assert service.engine.health_as_of("SYNTH_A", 100).state is state
    assert service.engine.canonical_state_bytes() == before
    with pytest.raises(TemporalConsistencyError):
        service.report_failure("SYNTH_A", 100, category)


def test_normalizer_requires_complete_records():
    service, (eid,) = seeded()
    ingest(service, [record(eid, 2)], 100)
    observation = service.engine.estimates(eid, EPS_DILUTED, 100).observations[0]
    parsed = parse_batch(payload([record(eid, 2)]), "SYNTH_A").items[0].records[0]
    revision = EventRevision(1, RevisionKind.ORIGINAL, 100)
    for incomplete in (
        replace(parsed, value=None),
        replace(parsed, estimate_type=None),
        replace(parsed, kind=RecordKind.ACTUAL),
        replace(parsed, kind=RecordKind.GUIDANCE),
    ):
        with pytest.raises(ValidationError, match="missing"):
            normalized_observation(incomplete, observation.source, revision)
    with pytest.raises(EIEError):
        quantity({"value": float("nan"), "unit": {"kind": "COUNT"}})


def test_valid_guidance_point_and_macro_previous_parse():
    _, (eid,) = seeded()
    row = record(
        eid,
        0,
        kind="GUIDANCE",
        measurement=GUIDANCE_EPS,
        vintage=None,
        published_at_ns=100,
        issuer_id=ISSUER,
        point={"value": 2, "unit": {"kind": "CURRENCY_PER_SHARE", "currency": "USD"}},
    )
    assert parse_batch(payload([row]), "SYNTH_A").items[0].records[0].point.value == 2
