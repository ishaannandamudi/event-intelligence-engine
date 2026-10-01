"""Fail-closed corporate contracts and synthetic provider schema drift."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from eie.config.settings import Settings
from eie.core.errors import SchemaCompatibilityError, TemporalConsistencyError, ValidationError
from eie.core.quality import ConfirmationState, QualityFlag
from eie.corporate.models import (
    Buyback,
    CorporateClaim,
    CorporateEvent,
    CorporateIdentity,
    CorporateKind,
    CorporateStatus,
    Deal,
    Delisting,
    Dividend,
    EarningsCall,
    EarningsPeriod,
    EarningsRelease,
    EarningsSchedule,
    ManagementChange,
    Money,
    ReleaseTiming,
    SchedulePrecision,
    SpinOff,
    Split,
    SymbolChange,
)
from eie.corporate.queries import (
    CorporateCoverage,
    CorporateQueryResult,
    CorporateSymbolQueryResult,
    CorporateView,
)
from eie.corporate.settings import CorporateSettings
from eie.entities.models import security_id
from eie.entities.protocols import ResolutionResult, ResolutionState
from eie.entities.sec import sec_issuer_id
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.corporate.adapter import (
    CorporateAdapter,
    CorporateFailureCategory,
    CorporateIngestor,
)
from eie.providers.corporate.normalization import canonical_identity, event_type_for
from eie.providers.corporate.parser import parse_batch
from eie.providers.sec.models import CIK
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass

FIXTURE = Path(__file__).parent / "fixtures" / "corporate" / "provider_a.json"
ISSUER = sec_issuer_id(CIK.parse(123456))
SID = security_id("FIGI", "CORP-TEST")
PERIOD = EarningsPeriod(2026, 3, "2026-09-30")
SCHEDULE = EarningsSchedule(
    PERIOD, "2026-10-20", None, ReleaseTiming.AFTER_MARKET, SchedulePrecision.DATE_PLUS_SESSION
)


def base_claim() -> CorporateClaim:
    return CorporateClaim(
        "SYNTH_A",
        "provider-event",
        "obs_claim",
        SourceClass.LICENSED_NEWS,
        100,
        SCHEDULE,
        CorporateStatus.SCHEDULED,
        50,
    )


def base_event() -> CorporateEvent:
    return CorporateEvent(
        CorporateIdentity(CorporateKind.EARNINGS_SCHEDULE, ISSUER, PERIOD.key),
        EventRevision(1, RevisionKind.ORIGINAL, 100),
        (base_claim(),),
        base_claim().key,
        ConfirmationState.REPORTED,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"fiscal_year": True},
        {"fiscal_year": 1899},
        {"fiscal_year": "2026"},
        {"fiscal_quarter": 0},
        {"fiscal_quarter": True},
        {"period_end": "bad"},
    ],
)
def test_earnings_period_requires_stable_fiscal_identity(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        replace(PERIOD, **change)
    assert PERIOD.key == "FY2026-Q3-2026-09-30"
    assert EarningsPeriod(2026, None, "2026-12-31").key == "FY2026-Q0-2026-12-31"


@pytest.mark.parametrize(
    "change",
    [
        {"period": "Q3"},
        {"scheduled_date": "bad"},
        {"release_timing": "AFTER_MARKET"},
        {"precision": "DATE_PLUS_SESSION"},
        {"precision": SchedulePrecision.EXACT_TIMESTAMP},
        {"scheduled_for_ns": 100},
        {"scheduled_date": None},
        {"release_timing": ReleaseTiming.UNKNOWN},
        {"precision": SchedulePrecision.UNKNOWN},
    ],
)
def test_earnings_schedule_rejects_fabricated_or_incomplete_time(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        replace(SCHEDULE, **change)
    assert (
        EarningsSchedule(
            PERIOD, None, None, ReleaseTiming.UNKNOWN, SchedulePrecision.UNKNOWN
        ).scheduled_date
        is None
    )
    assert (
        EarningsSchedule(
            PERIOD, "2026-10-20", 100, ReleaseTiming.UNKNOWN, SchedulePrecision.EXACT_TIMESTAMP
        ).scheduled_for_ns
        == 100
    )
    with pytest.raises(ValidationError):
        EarningsSchedule(PERIOD, None, None, ReleaseTiming.AFTER_MARKET, SchedulePrecision.UNKNOWN)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Money(-1, "USD"),
        lambda: Money(True, "USD"),
        lambda: Money(1, "usd"),
        lambda: Money(1, "US"),
        lambda: Money(1, "USD", "yes"),
        lambda: EarningsRelease("Q3", 10),
        lambda: EarningsRelease(PERIOD, -1),
        lambda: EarningsCall("Q3", 10),
        lambda: EarningsCall(PERIOD, -1),
        lambda: EarningsCall(PERIOD, 10, "http://example.com"),
        lambda: EarningsCall(PERIOD, 10, "https://u:p@example.com/x"),
        lambda: EarningsCall(PERIOD, 10, "https://example.com/x?token=secret"),
        lambda: Dividend(Money(1, "USD", False)),
        lambda: Dividend(ex_date="not-a-date"),
        lambda: Split(0, 1),
        lambda: Split(1, 0),
        lambda: Split(1, 2, effective_date="bad"),
        lambda: Buyback(authorization=Money(1, "USD", True)),
        lambda: Buyback(shares=-1),
        lambda: Buyback(percent_basis_points=10001),
        lambda: Buyback(expanded="yes"),
        lambda: Deal(None, None),
        lambda: Deal(ISSUER, None, unresolved_party_name=" "),
        lambda: Deal(ISSUER, None, actual_close_date="bad"),
        lambda: Deal(ISSUER, None, cash_component=Money(1, "USD", True)),
        lambda: Deal(ISSUER, None, stock_component_shares=-1),
        lambda: SpinOff(ratio_numerator=1),
        lambda: SpinOff(ratio_numerator=0, ratio_denominator=1),
        lambda: SymbolChange("bad", "OLD", "NYSE", "NEW", "NYSE", 10),
        lambda: SymbolChange(SID, "OLD", "NYSE", "OLD", "NYSE", 10),
        lambda: SymbolChange(SID, "OLD", "NYSE", "NEW", "NYSE", 0),
        lambda: SymbolChange(SID, "bad symbol", "NYSE", "NEW", "NYSE", 10),
        lambda: Delisting("bad", "NYSE"),
        lambda: Delisting(SID, "bad symbol"),
        lambda: Delisting(SID, "NYSE", reason=" "),
        lambda: ManagementChange("", "CEO"),
        lambda: ManagementChange("Ada", ""),
        lambda: ManagementChange("Ada", "CEO", source_person_id=" "),
        lambda: ManagementChange("Ada", "CEO", effective_date="bad"),
    ],
)
def test_structured_payload_validation_rejects_invalid_data(build) -> None:
    with pytest.raises(ValidationError):
        build()


def test_tender_deal_and_management_valid_optional_fields() -> None:
    from eie.corporate.models import TenderOffer

    assert TenderOffer(Money(5_000_000, "USD", True), 100, "issuer").expiration_at_ns == 100
    with pytest.raises(ValidationError):
        TenderOffer(Money(1, "USD", False))
    with pytest.raises(ValidationError):
        TenderOffer(tender_type=" ")
    assert (
        Deal(ISSUER, None, actual_close_at_ns=100, terminated_at_ns=200).actual_close_at_ns == 100
    )
    assert ManagementChange("Ada", "CEO", "person-1", "2026-11-01").source_person_id == "person-1"


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "EARNINGS_SCHEDULE"},
        {"issuer_id": "bad"},
        {"semantic_key": ""},
        {"semantic_key": " bad "},
        {"security_id": "bad"},
        {"listing_id": "lst_bad"},
    ],
)
def test_corporate_identity_is_stable_and_typed(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        replace(base_event().identity, **change)
    assert base_event().identity.corporate_id == base_event().identity.corporate_id


@pytest.mark.parametrize(
    "change",
    [
        {"provider_id": "bad provider"},
        {"provider_event_id": ""},
        {"observation_id": "bad"},
        {"source_class": "LICENSED_NEWS"},
        {"status": "SCHEDULED"},
        {"payload": "bad"},
        {"available_from_ns": -1},
        {"announced_at_ns": 101},
        {"quality_flags": (QualityFlag.STALE,)},
    ],
)
def test_corporate_claim_requires_provenance_and_time(change: dict[str, object]) -> None:
    with pytest.raises((ValidationError, TemporalConsistencyError)):
        replace(base_claim(), **change)
    assert base_claim().result_fingerprint == replace(base_claim()).result_fingerprint


def test_corporate_event_rejects_conflicting_or_mismatched_claims() -> None:
    base = base_event()
    with pytest.raises(SchemaCompatibilityError):
        replace(base, schema_version="2.0")
    for change in (
        {"identity": "bad"},
        {"claims": ()},
        {"selected_claim_key": ("SYNTH_B", "missing")},
        {"claims": (base.claims[0], base.claims[0])},
        {"claims": (replace(base.claims[0], payload=Dividend()),)},
        {"confirmation": "REPORTED"},
        {"conflict_reason_codes": ("Z", "A")},
        {"claims": (replace(base.claims[0], available_from_ns=101),)},
        {"identity": replace(base.identity, semantic_key="wrong-period")},
        {"claims": (replace(base.claims[0], status=CorporateStatus.ANNOUNCED),)},
    ):
        with pytest.raises((ValidationError, TemporalConsistencyError)):
            replace(base, **change)
    competing = replace(
        base.claims[0],
        provider_id="SYNTH_B",
        provider_event_id="other",
        payload=replace(SCHEDULE, scheduled_date="2026-10-21"),
    )
    with pytest.raises(ValidationError):
        replace(
            base, claims=tuple(sorted((base.claims[0], competing), key=lambda claim: claim.key))
        )
    with pytest.raises(ValidationError):
        replace(base, conflict_reason_codes=("CONFLICT",))
    assert base.result_fingerprint == replace(base).result_fingerprint
    assert base.corporate_id == base.identity.corporate_id
    assert base.selected_claim == base.claims[0]


def test_split_direction_and_symbol_security_are_enforced_by_event_contract() -> None:
    for kind, split in (
        (CorporateKind.STOCK_SPLIT, Split(1, 10)),
        (CorporateKind.REVERSE_SPLIT, Split(4, 1)),
    ):
        claim = replace(base_claim(), payload=split, status=CorporateStatus.ANNOUNCED)
        identity = CorporateIdentity(kind, ISSUER, "split-key")
        with pytest.raises(ValidationError):
            CorporateEvent(
                identity, base_event().revision, (claim,), claim.key, ConfirmationState.REPORTED
            )
    change = SymbolChange(SID, "OLD", "NYSE", "NEW", "NYSE", 100)
    claim = replace(base_claim(), payload=change, status=CorporateStatus.ANNOUNCED)
    identity = CorporateIdentity(CorporateKind.SYMBOL_CHANGE, ISSUER, "symbol-key")
    with pytest.raises(ValidationError):
        CorporateEvent(
            identity, base_event().revision, (claim,), claim.key, ConfirmationState.REPORTED
        )


def test_query_results_coverage_and_fingerprints_validate_knowledge() -> None:
    corporate = base_event()
    canonical = CanonicalEvent(
        canonical_identity(corporate.identity),
        corporate.revision,
        LifecycleState.SCHEDULED,
        ConfirmationState.REPORTED,
        ("obs_claim",),
        frozenset({SourceClass.LICENSED_NEWS}),
        "Earnings",
    )
    view = CorporateView(corporate, canonical, 100)
    coverage = CorporateCoverage(
        "SYNTH_A", (CorporateKind.EARNINGS_SCHEDULE,), "2026-10-01", "2026-10-31", 100
    )
    query = CorporateQueryResult(100, (view,), (coverage,), "config")
    assert query.result_fingerprint == replace(query).result_fingerprint
    assert view.result_fingerprint == replace(view).result_fingerprint
    symbol = CorporateSymbolQueryResult(
        100, ResolutionResult(ResolutionState.RESOLVED, (ISSUER,), "exact"), query
    )
    assert symbol.result_fingerprint == replace(symbol).result_fingerprint
    with pytest.raises(TemporalConsistencyError):
        CorporateView(corporate, canonical, 99)
    with pytest.raises(ValidationError):
        CorporateView(
            corporate,
            replace(canonical, revision=EventRevision(2, RevisionKind.UPDATE, 101, 1)),
            101,
        )
    with pytest.raises(ValidationError):
        CorporateQueryResult(100, (view, view), (), "config")
    with pytest.raises(ValidationError):
        CorporateQueryResult(101, (view,), (), "config")
    with pytest.raises(TemporalConsistencyError):
        CorporateQueryResult(99, (), (coverage,), "config")
    with pytest.raises(ValidationError):
        replace(coverage, from_date="bad")
    with pytest.raises(ValidationError):
        replace(coverage, from_date="2027-01-01")
    with pytest.raises(ValidationError):
        replace(coverage, kinds=(CorporateKind.EARNINGS_SCHEDULE, CorporateKind.EARNINGS_SCHEDULE))


def test_settings_and_adapter_reject_unsafe_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(corporate="bad")
    for change in (
        {"enabled": 1},
        {"max_payload_bytes": 0},
        {"max_items_per_batch": True},
        {"max_results": 0},
        {"staleness_ns": 0},
    ):
        with pytest.raises(ValidationError):
            replace(CorporateSettings(), **change)
    with pytest.raises(ValidationError):
        CorporateAdapter("bad", SourceClass.LICENSED_NEWS, frozenset(CorporateKind))
    with pytest.raises(ValidationError):
        CorporateAdapter("SYNTH_A", "LICENSED_NEWS", frozenset(CorporateKind))
    with pytest.raises(ValidationError):
        CorporateAdapter("SYNTH_A", SourceClass.LICENSED_NEWS, (CorporateKind.DIVIDEND,))


def test_parser_drift_and_harmless_extra_fields() -> None:
    extra = json.loads(FIXTURE.read_bytes())
    extra["futureField"] = {"safe": True}
    assert (
        parse_batch(json.dumps(extra).encode(), "SYNTH_A").items
        == parse_batch(FIXTURE.read_bytes(), "SYNTH_A").items
    )
    with pytest.raises(ProviderParseError):
        parse_batch(b"bad JSON", "SYNTH_A")
    for mutate in (
        lambda x: x.update({"schema": "bad"}),
        lambda x: x.update({"provider_id": "SYNTH_B"}),
        lambda x: x.pop("items"),
        lambda x: x["items"][0].pop("events"),
        lambda x: x["items"][0]["events"][0].pop("kind"),
        lambda x: x["items"][0]["events"][0]["data"].update({"scheduled_for_ns": "tomorrow"}),
        lambda x: x["items"][0]["events"][0].update({"issuer_id": "bad"}),
        lambda x: x["items"][0].update({"source_reference": "http://example.com"}),
        lambda x: x["items"][0].update({"source_reference": "https://[broken"}),
        lambda x: x["items"][0].update({"published_at_ns": "now"}),
        lambda x: x["coverage"].update({"to_date": "bad"}),
        lambda x: x["coverage"].update({"kinds": ["BAD"]}),
    ):
        data = json.loads(FIXTURE.read_bytes())
        mutate(data)
        with pytest.raises(
            (ValidationError, ProviderSchemaMismatchError, TemporalConsistencyError)
        ):
            parse_batch(json.dumps(data).encode(), "SYNTH_A")
    with pytest.raises(ValidationError):
        EarningsCall(PERIOD, 100, "https://[broken")


def test_earnings_release_identity_and_actual_publication_are_distinct() -> None:
    from eie.entities.sec import SecEntityRegistry, SecIssuerRevision

    cik = CIK.parse(123456)
    reg = SecEntityRegistry()
    reg.add_issuer(SecIssuerRevision(ISSUER, cik, "Alpha", (), 1, "obs_issuer"))
    service = CorporateIngestor(Settings(corporate=CorporateSettings(enabled=True)), reg)
    source = CorporateAdapter("SYNTH_A", SourceClass.COMPANY_PRIMARY, frozenset(CorporateKind))
    stamp = 1_788_264_000_000_000_000
    event = {
        "kind": "EARNINGS_RELEASE",
        "provider_event_id": "release-q3",
        "issuer_id": ISSUER,
        "status": "ANNOUNCED",
        "data": {
            "fiscal_year": 2026,
            "fiscal_quarter": 3,
            "period_end": "2026-09-30",
            "published_at_ns": stamp,
        },
    }
    batch = {
        "schema": "EIE_CORPORATE_EXTRACT_V1",
        "provider_id": "SYNTH_A",
        "source_version": "synthetic-1",
        "items": [
            {
                "item_id": "release",
                "published_at_ns": stamp,
                "source_reference": "https://example.com/release",
                "events": [event],
            }
        ],
    }
    service.ingest_payload(source, json.dumps(batch).encode(), stamp, stamp)
    view = service.index.known_as_of(stamp, kind=CorporateKind.EARNINGS_RELEASE).events[0]
    assert view.canonical.published_at_ns == stamp
    assert view.canonical.scheduled_for_ns is None
    assert event_type_for(CorporateKind.EARNINGS_RELEASE).code == "EARNINGS.RELEASE"
    assert view.corporate.identity.semantic_key == PERIOD.key
    batch["items"][0]["events"][0]["data"]["published_at_ns"] = stamp + 1
    assert (
        service.ingest_payload(
            source, json.dumps(batch).encode(), stamp + 2, stamp + 2
        ).events_created_or_updated
        == 1
    )
    assert service.index.known_as_of(stamp).events[0].canonical.published_at_ns == stamp


def test_reported_source_failures_distinguish_empty_and_unavailable() -> None:
    service = CorporateIngestor(Settings(corporate=CorporateSettings(enabled=True)))
    result = service.report_failure("SYNTH_A", 10, CorporateFailureCategory.RATE_LIMITED)
    assert result.health.state is SourceHealthState.RATE_LIMITED
    assert service.health_as_of("SYNTH_A", 9).state is SourceHealthState.UNKNOWN
    with pytest.raises(TemporalConsistencyError):
        service.report_failure("SYNTH_A", 10, CorporateFailureCategory.UNAVAILABLE)
    with pytest.raises(ValidationError):
        CorporateIngestor(Settings()).report_failure(
            "SYNTH_A", 11, CorporateFailureCategory.UNAVAILABLE
        )
