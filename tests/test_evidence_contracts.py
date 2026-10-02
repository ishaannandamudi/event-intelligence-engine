"""Typed identities, evidence locations and inert serialization boundaries."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest

from eie.backfill.models import ProvenanceKind, ReconstructionQuality
from eie.core.errors import PersistenceError, ValidationError
from eie.corporate.actions import Dividend
from eie.corporate.earnings import EarningsSchedule
from eie.corporate.types import ReleaseTiming, SchedulePrecision
from eie.events.lifecycle import LifecycleState
from eie.evidence import CLAIM_EVIDENCE_SCHEMA_VERSION
from eie.evidence.claims import (
    ActionValue,
    Claim,
    ClaimSubject,
    IdentityValue,
    NumericRange,
    OccurrenceValue,
    RelationshipValue,
    StatusValue,
    TimePrecision,
    TimeValue,
)
from eie.evidence.content import verify_fragments
from eie.evidence.graph import ClaimRelationship
from eie.evidence.matching import DedupePolicy, EventParty, PartyRole
from eie.evidence.models import EvidenceLimits, ResolutionPolicy
from eie.evidence.references import (
    EvidenceDataset,
    EvidenceLocator,
    LocatorKind,
    SpanKind,
)
from eie.evidence.repository import limit_check, typed
from eie.evidence.resolution import compatible, conflict_kind
from eie.evidence.schema import ClaimType, ConflictType, EvidenceRelation, Predicate, SubjectKind
from eie.expectations.models import EPS_DILUTED, MeasurementContext
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.graph.taxonomy import RelationshipType
from eie.persistence.codec import dumps, loads
from eie.providers.evidence.adapter import parse
from eie.providers.evidence.normalization import (
    earnings_time_claim,
    field_reference,
    reprocessing_lineage,
)
from tests.evidence_helpers import CLAIM, ISSUER, PERIOD, TIME, item, source


def test_semantic_and_provider_identity():
    a, b = item(source("ALPHA"), 100), item(source("BETA"), 100)
    assert a.claims[0].claim.claim_id == b.claims[0].claim.claim_id
    assert a.raw.observation_id != b.raw.observation_id
    assert a.references[0].evidence_id != b.references[0].evidence_id
    assert replace(CLAIM, predicate=Predicate.SCHEDULED_FOR).claim_id == CLAIM.claim_id
    assert CLAIM_EVIDENCE_SCHEMA_VERSION == "1.0"
    assert parse(dumps(a)) == a
    assert loads(dumps(a)) == a
    with pytest.raises(FrozenInstanceError):
        a.resource_id = "other"


@pytest.mark.parametrize(
    "kind,prefix",
    [
        (SubjectKind.EVENT, "evt"),
        (SubjectKind.ENTITY, "iss"),
        (SubjectKind.ENTITY, "sec"),
        (SubjectKind.ENTITY, "lst"),
        (SubjectKind.ENTITY, "node"),
        (SubjectKind.FILING, "fil"),
        (SubjectKind.RELATIONSHIP, "edge"),
        (SubjectKind.CORPORATE, "corp"),
        (SubjectKind.MEASUREMENT, "evt"),
    ],
)
def test_typed_subjects(kind, prefix):
    value = ClaimSubject(kind, prefix + "_" + "a" * 64)
    assert loads(dumps(value)) == value
    with pytest.raises(ValidationError):
        ClaimSubject(kind, "display-name")


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_type": "EARNINGS_SCHEDULE"},
        {"predicate": Predicate.ACTUAL},
        {"schema_version": "9"},
        {"measurement": MeasurementContext("evt_" + "a" * 64, EPS_DILUTED, PERIOD)},
        {"subject": None},
    ],
)
def test_invalid_claim_contract(changes):
    with pytest.raises(ValidationError):
        replace(CLAIM, **changes)


def test_numeric_contract_and_compatible_units():
    context = MeasurementContext("evt_" + "a" * 64, EPS_DILUTED, PERIOD)
    dollars = Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD"))
    claim = Claim(
        ClaimType.ACTUAL_VALUE,
        ClaimSubject(SubjectKind.MEASUREMENT, context.event_id),
        Predicate.ACTUAL,
        context,
        dollars.canonical_unit,
    )
    claim.validate_value(dollars)
    claim.validate_value(NumericRange(dollars, dollars))
    assert compatible(dollars, dollars)
    wrong = Quantity(1, Unit(UnitKind.COUNT))
    with pytest.raises(ValidationError):
        claim.validate_value(wrong)
    assert conflict_kind(dollars, wrong) is ConflictType.UNIT
    with pytest.raises(ValidationError):
        NumericRange(dollars, wrong)
    with pytest.raises(ValidationError):
        NumericRange(Quantity(2, dollars.unit), dollars)
    with pytest.raises(ValidationError):
        replace(claim, subject=ClaimSubject(SubjectKind.MEASUREMENT, "evt_" + "b" * 64))
    with pytest.raises(ValidationError):
        Claim(ClaimType.ACTUAL_VALUE, claim.subject, Predicate.ACTUAL)
    with pytest.raises(ValidationError):
        CLAIM.validate_value(dollars)


@pytest.mark.parametrize("precision", list(SchedulePrecision))
def test_earnings_precision_bridge(precision):
    schedule = EarningsSchedule(
        PERIOD,
        None if precision is SchedulePrecision.UNKNOWN else TIME.date,
        TIME.timestamp_ns if precision is SchedulePrecision.EXACT_TIMESTAMP else None,
        ReleaseTiming.AFTER_MARKET
        if precision is SchedulePrecision.DATE_PLUS_SESSION
        else ReleaseTiming.UNKNOWN,
        precision,
    )
    claim, value = earnings_time_claim(CLAIM.subject, schedule)
    assert claim == CLAIM
    assert value.precision.value in {"UNKNOWN", "DATE_ONLY", "DATE_PLUS_SESSION", "EXACT"}
    assert value.timestamp_ns is None or value.timestamp_ns == TIME.timestamp_ns


@pytest.mark.parametrize(
    "changes",
    [
        {"date": "2026-2-3"},
        {"date": None},
        {"date": "2026-10-29"},
        {"timestamp_ns": None},
        {"timestamp_ns": True},
        {"precision": TimePrecision.DATE_ONLY},
        {"precision": TimePrecision.UNKNOWN},
        {"precision": "EXACT"},
    ],
)
def test_invalid_time(changes):
    with pytest.raises(ValidationError):
        replace(TIME, **changes)


def test_time_refinement_and_status_objects():
    coarse = TimeValue(TimePrecision.DATE_ONLY, TIME.date)
    assert compatible(coarse, TIME)
    assert not compatible(coarse, TimeValue(TimePrecision.DATE_ONLY, "2026-10-29"))
    assert not compatible(TIME, replace(TIME, timestamp_ns=TIME.timestamp_ns + 1))
    assert compatible(TimeValue(TimePrecision.UNKNOWN), TimeValue(TimePrecision.UNKNOWN))
    assert not compatible(TimeValue(TimePrecision.UNKNOWN), TIME)
    with pytest.raises(ValidationError):
        TimeValue(TimePrecision.DATE_PLUS_SESSION, TIME.date)
    with pytest.raises(ValidationError):
        TimeValue(TimePrecision.DATE_ONLY, TIME.date, session=ReleaseTiming.AFTER_MARKET)
    state = StatusValue(LifecycleState.SCHEDULED)
    assert conflict_kind(state, StatusValue(LifecycleState.RELEASED)) is ConflictType.STATUS
    identity = IdentityValue(ClaimSubject(SubjectKind.ENTITY, ISSUER))
    assert conflict_kind(identity, identity) is ConflictType.ENTITY
    assert ActionValue(Dividend()).payload.amount is None
    assert OccurrenceValue(False).occurred is False
    with pytest.raises(ValidationError):
        RelationshipValue(RelationshipType.SUPPLIER_TO, CLAIM.subject, CLAIM.subject)
    other = ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64)
    relation = RelationshipValue(RelationshipType.SUPPLIER_TO, identity.target, other)
    assert loads(dumps(relation)) == relation
    assert EventParty(PartyRole.TARGET, other).entity == other
    with pytest.raises(ValidationError):
        EventParty(PartyRole.TARGET, CLAIM.subject)


@pytest.mark.parametrize(
    "locator",
    [
        EvidenceLocator(LocatorKind.FIELD, "value"),
        EvidenceLocator(LocatorKind.JSON_POINTER, "/value"),
        EvidenceLocator(LocatorKind.SEC_METADATA, "value"),
        EvidenceLocator(LocatorKind.CSV_FIELD, "value", row=0),
        EvidenceLocator(LocatorKind.XML_PATH, "/root/value"),
    ],
)
def test_precise_fragment_verification(locator):
    payload = b'{"value":"yes"}'
    if locator.kind is LocatorKind.CSV_FIELD:
        payload = b"value\nyes\n"
    if locator.kind is LocatorKind.XML_PATH:
        payload = b"<root><value>yes</value></root>"
    batch = item(source("ALPHA"), 100)
    ref = field_reference(
        batch.raw.observation_id, "item", sha256(payload).hexdigest(), "1", "1", locator, "yes"
    )
    verify_fragments(payload, (ref,))
    with pytest.raises(ValidationError):
        verify_fragments(payload, (replace(ref, fragment_hash="b" * 64),))
    assert replace(ref, fragment_hash="b" * 64).evidence_id != ref.evidence_id


@pytest.mark.parametrize("kind", list(SpanKind))
def test_span_verification(kind):
    payload = "éyes".encode()
    text = payload if kind is SpanKind.BYTE else payload.decode()
    locator = EvidenceLocator(
        LocatorKind.FIELD, "fragment", span_kind=kind, start=0, end=1, content_length=len(text)
    )
    batch = item(source("ALPHA"), 100)
    ref = replace(
        batch.references[0],
        locator=locator,
        fragment_hash=sha256(payload[:1] if kind is SpanKind.BYTE else "é".encode()).hexdigest(),
    )
    verify_fragments(payload, (ref,))
    with pytest.raises(ValidationError):
        verify_fragments(payload, (replace(ref, locator=replace(locator, content_length=20)),))


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": LocatorKind.JSON_POINTER, "path": "field"},
        {"kind": LocatorKind.XML_PATH, "path": "field"},
        {"kind": LocatorKind.CSV_FIELD},
        {"row": 0},
        {"start": 0},
        {"span_kind": SpanKind.BYTE},
        {"span_kind": SpanKind.BYTE, "start": 0, "end": 2, "content_length": 1},
        {"row": -1},
    ],
)
def test_invalid_locators(changes):
    with pytest.raises(ValidationError):
        replace(EvidenceLocator(LocatorKind.FIELD, "value"), **changes)


@pytest.mark.parametrize(
    "payload,path",
    [
        (b"{}", "/missing"),
        (b'{"value":1,"value":2}', "/value"),
        (b'{"value":1}', "/value/x"),
        (b'{"value":[1]}', "/value/3"),
        (b"broken", "/value"),
    ],
)
def test_unlocatable_fragments(payload, path):
    ref = replace(
        item(source("ALPHA"), 100).references[0],
        locator=EvidenceLocator(LocatorKind.JSON_POINTER, path),
    )
    with pytest.raises((ValidationError, PersistenceError)):
        verify_fragments(payload, (ref,))


def test_lineage_bounds_and_reprocessing():
    batch = item(source("ALPHA"), 100)
    assert batch.lineage.known_from_ns == 100
    assert reprocessing_lineage(batch.lineage, "2", "b" * 64).lineage_id != batch.lineage.lineage_id
    for changes in (
        {"observed_at_ns": 101},
        {"source_available_ns": 1},
        {"build_id": "build"},
        {"settings_fingerprint": "bad"},
        {"provenance": ProvenanceKind.HISTORICALLY_RECONSTRUCTED},
        {"raw_observation_id": "source"},
    ):
        with pytest.raises(ValidationError):
            replace(batch.lineage, **changes)
    reconstructed = replace(
        batch.lineage,
        provenance=ProvenanceKind.HISTORICALLY_RECONSTRUCTED,
        build_id="build",
        historical_fact_id="fact",
        historical_acquisition_id="acquisition",
        source_available_ns=10,
    )
    assert reconstructed.known_from_ns == 10
    with pytest.raises(ValidationError):
        replace(reconstructed, source_available_ns=101)
    with pytest.raises(ValidationError):
        replace(reconstructed, reconstruction=ReconstructionQuality.UNKNOWN)
    assert (
        replace(
            reconstructed, reconstruction=ReconstructionQuality.UNKNOWN, source_available_ns=None
        ).known_from_ns
        == 100
    )
    with pytest.raises(ValidationError):
        EvidenceDataset("bad", "hash")


@pytest.mark.parametrize("value", [0, -1, True, 100001, "100"])
def test_query_limits(value):
    with pytest.raises(ValidationError):
        limit_check(value)


@pytest.mark.parametrize("field", list(EvidenceLimits.__dataclass_fields__))
def test_configured_limit_bounds(field):
    with pytest.raises(ValidationError):
        replace(EvidenceLimits(), **{field: 0})


def test_static_adapter_and_policy_security():
    batch = item(source("ALPHA"), 100)
    for payload in (dumps(CLAIM), b'{"type":"os.system","fields":{}}', b"not json"):
        with pytest.raises((PersistenceError, ValidationError)):
            parse(payload)
    with pytest.raises(ValidationError):
        parse(dumps(batch), max_bytes=1)
    with pytest.raises(ValidationError):
        parse("string")
    with pytest.raises(PersistenceError):
        typed("bad", Claim)
    with pytest.raises(PersistenceError):
        typed(dumps(batch), Claim)
    assert ResolutionPolicy().fingerprint != ResolutionPolicy(prefer_exact_time=False).fingerprint
    for tolerances in ((("type", -1),), (("type", 1), ("type", 1)), (("z", 1), ("a", 1))):
        with pytest.raises(ValidationError):
            DedupePolicy(tolerances)
    with pytest.raises(ValidationError):
        ClaimRelationship(
            "pclaim_" + "a" * 64, "pclaim_" + "a" * 64, EvidenceRelation.SUPPORTS, 100
        )
